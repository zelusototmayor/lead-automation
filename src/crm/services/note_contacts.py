"""Cited contact maintenance in the existing note-plan transaction."""
from datetime import UTC, datetime
from uuid import UUID, uuid5
import unicodedata
from sqlalchemy import select
from src.crm.persistence.models import AgentWork, AuditEvent, Contact, SourceIdentity
from src.crm.services.agent_work_service import WorkConflict
from src.crm.connectors.note_drafts import valid_address

def normalized_name(value):
    text=unicodedata.normalize('NFKD',value or '')
    return ' '.join(''.join(c for c in text if not unicodedata.combining(c)).casefold().split())


def reconciled_name(existing, incoming):
    """A cited first name can enrich an identified person without dropping a surname."""
    if not existing:return incoming
    old,new=normalized_name(existing),normalized_name(incoming)
    if old==new or old.startswith(new+' '):return existing
    if new.startswith(old+' '):return incoming
    raise WorkConflict('Named contact identity requires reconciliation')


def _resolve_contact(session, principal, lead, work_id, i, change, contacts):
    """Shared identity/creation writer; caller holds locks and validates evidence."""
    matches=[c for c in contacts if change.email and str(c.primary_email or '').casefold()==change.email.casefold()]
    if change.contact_id:
        contact=next((c for c in contacts if c.id==change.contact_id),None)
        if not contact or contact.version!=change.expected_contact_version:
            raise WorkConflict('Contact changed or belongs to another company')
        if matches and matches[0].id!=contact.id:raise WorkConflict('Email belongs to a different contact')
    else:
        if not matches and change.full_name:
            matches=[c for c in contacts if normalized_name(c.full_name)==normalized_name(change.full_name)]
        if len(matches)>1:raise WorkConflict('Ambiguous same-name contact')
        contact=matches[0] if matches else None
    before={k:getattr(contact,k) for k in ('full_name','title','primary_email','phone','is_primary')} if contact else None
    if change.email:
        other=session.scalar(select(Contact).where(Contact.workspace_id==principal.workspace_id,
            Contact.primary_email==change.email,Contact.account_id!=lead.account_id))
        if other:raise WorkConflict('Email is already assigned to another company')
    if contact is None:
        contact=Contact(id=uuid5(work_id,'note-contact:'+str(i)),workspace_id=principal.workspace_id,
            account_id=lead.account_id,status='active',is_primary=False)
        session.add(contact);contacts.append(contact)
    return contact, before


def apply_contacts(session, principal, lead, work_id, source, updates):
    if not updates:return []
    if not lead.account_id:raise WorkConflict('Contact requires an existing company')
    contacts=list(session.scalars(select(Contact).where(Contact.workspace_id==principal.workspace_id,
        Contact.account_id==lead.account_id).order_by(Contact.id).with_for_update(nowait=True)))
    text=source.get('summary') or ''
    results=[]; primary_count=0
    for i,change in enumerate(updates):
        if not change.quote.strip() or change.quote not in text:
            raise WorkConflict('Contact evidence is not in the source note')
        values={k:getattr(change,k) for k in ('full_name','title','email','phone') if getattr(change,k) is not None}
        if not values:raise WorkConflict('Empty contact update')
        for key,value in values.items():
            if not value.strip() or value.casefold() not in text.casefold():
                raise WorkConflict('Contact value must be literal source evidence')
            if key=='email':valid_address(value)
        contact, before = _resolve_contact(session, principal, lead, work_id, i, change, contacts)
        if values.get('full_name') and before and before['full_name']:
            values['full_name']=reconciled_name(before['full_name'],values['full_name'])
        for key,value in values.items():setattr(contact,'primary_email' if key=='email' else key,value)
        if change.make_primary:
            primary_count+=1
            if primary_count>1:raise WorkConflict('Only one primary contact per note')
            for c in contacts:c.is_primary=c.id==contact.id
            lead.contact_id=contact.id
        if lead.contact_id==contact.id:
            lead.contact_name=contact.full_name
            lead.contact_email=contact.primary_email
            lead.contact_phone=contact.phone
        contact.updated_at=datetime.now(UTC)
        link_id=uuid5(lead.id,'note-contact-link:'+str(contact.id))
        link=session.get(SourceIdentity,link_id)
        if link is None:
            session.add(SourceIdentity(id=link_id,workspace_id=principal.workspace_id,source_system='agent',
                entity_kind='document',source_scope='note-contact-link/v1',external_id=str(lead.id)+':'+str(contact.id),
                canonical_entity_type='contact',canonical_entity_id=contact.id,
                metadata_json={'lead_id':str(lead.id),'account_id':str(lead.account_id),'source_activity_id':source['activity_id']}))
        results.append({'contact_id':str(contact.id),'lead_id':str(lead.id),'created':before is None,
            'before':before,'after':{k:getattr(contact,k) for k in ('full_name','title','primary_email','phone','is_primary')},
            'source_activity_id':source['activity_id'],'quote':change.quote})
    lead.updated_at=datetime.now(UTC)
    session.flush()
    by_id={str(c.id):c for c in contacts}
    for result in results:
        result['version']=by_id[result['contact_id']].version
    return results


def expand_contact_receipt(session, workspace_id, receipt):
    """Materialize applied values from bounded per-contact audit records."""
    result = dict(receipt)
    result.pop('change_audit_ids', None)
    changes = []
    for audit_id in receipt.get('change_audit_ids', []):
        event = session.get(AuditEvent, UUID(audit_id))
        if not event or event.workspace_id != workspace_id or event.action != 'agent.contact_from_note':
            raise WorkConflict('Contact phase audit is incomplete')
        change = event.details
        after = change['after']
        changes.append({'contact_id': change['contact_id'], 'version': change['version'],
            'name': after['full_name'], 'title': after.get('title'),
            'email': str(after['primary_email']) if after['primary_email'] else None,
            'phone': after['phone'], 'is_primary': after['is_primary'],
            'created': change['created'], 'quote': change['quote']})
    result['actual_changes'] = changes
    return result


def latest_contact_phase(session, workspace_id, work_id, source):
    """Only an existing canonical audit for this exact note can satisfy its phase."""
    if source.get('status') != 'current':
        return None
    event = session.scalar(select(AuditEvent).where(
        AuditEvent.workspace_id == workspace_id,
        AuditEvent.entity_type == 'agent_work', AuditEvent.entity_id == work_id,
        AuditEvent.action == 'agent.note_contacts_completed',
        AuditEvent.details['receipt']['source_digest'].as_string() == source['source_digest'],
        AuditEvent.details['receipt']['source_activity_id'].as_string() == source['activity_id'],
    ).order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc()).limit(1))
    if not event:
        return None
    receipt = expand_contact_receipt(session, workspace_id, event.details['receipt'])
    verified = bool(receipt['actual_changes'])
    for change in receipt['actual_changes']:
        contact = session.get(Contact, UUID(change['contact_id']))
        verified = verified and bool(contact and contact.workspace_id == workspace_id
            and contact.version == change['version']
            and contact.full_name == change['name'] and contact.title == change['title']
            and str(contact.primary_email or '') == str(change['email'] or '')
            and contact.phone == change['phone'] and contact.is_primary == change['is_primary'])
    work = session.get(AgentWork, work_id)
    if work and work.workspace_id == workspace_id and work.lead_id:
        from src.crm.services.contact_channels import operational_context
        current = operational_context(session, workspace_id, work.lead_id)
        verified = verified and current['operational_contact_id'] == receipt.get('operational_contact_id')
    else:
        verified = False
    receipt['verified'] = verified
    return receipt
