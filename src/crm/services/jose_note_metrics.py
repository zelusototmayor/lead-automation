"""Read-only exact-source counts for José's personal-note policy.

No legacy usefulness/role fallback. No notes, names or identifiers in the public
aggregate. Similar text is not deduplicated; revisions are not extra calls.
"""
from collections import defaultdict, Counter
import os
from datetime import UTC, datetime, date
from zoneinfo import ZoneInfo
from sqlalchemy import select
from src.crm.persistence.models import Activity, AuditEvent, Lead
from src.crm.services.jose_note_service import ACTION, WITHDRAW, current_audit, audit_classification
from src.crm.services.jose_note_policy_v2 import IMPORT_CONVENTION_VERSION as VERSION
from src.crm.services.jose_note_service import V19_VERSION


def enabled():
    return (os.environ.get('CRM_JEV_PERSONAL_NOTES_ENABLED') == '1'
        or os.environ.get('CRM_V19_PERSONAL_NOTES_ENABLED') == '1')


def metrics(session, workspace_id, *, anchor_date, period='day', index=None):
    from src.crm.services.commercial_kpi_service import period_bounds, SourceIndex
    start,end=period_bounds(anchor_date,period)
    week_start,week_end=period_bounds(anchor_date,'week')
    calendar=ZoneInfo('Europe/Lisbon')
    first_day,last_day=(t.astimezone(calendar).date() for t in (start,end))
    first_week,last_week=(t.astimezone(calendar).date() for t in (week_start,week_end))
    index=index or SourceIndex(session,workspace_id)
    rows=list(index.activities.values())
    superseded=index.superseded
    audits=defaultdict(list)
    for audit in session.scalars(select(AuditEvent).where(AuditEvent.workspace_id==workspace_id,
            AuditEvent.entity_type=='activity',AuditEvent.action.in_((ACTION,WITHDRAW)))):
        audits[audit.entity_id].append(audit)
    counts=dict.fromkeys(('calls','recovered_note_calls','answered','no_answer','answer_unknown',
        'gatekeeper','relevant','first_recorded','follow_up','order_unknown','meetings','callbacks'),0)
    coverage=dict.fromkeys(('sources','empty','unclassified','call_unknown','noncall','superseded','recording_date_only',
        'technical_mirrors','excluded','identity_conflict'),0)
    calls=[];weekly=0;policy_versions=set()
    for source in rows:
        if source.activity_type not in ('call','note'):continue
        in_period=start<=source.occurred_at<end
        if source.id in superseded:
            coverage['superseded']+=in_period;continue
        if source.id in index.duplicates:
            coverage['technical_mirrors']+=in_period;continue
        if source.id in index.exclusions:
            coverage['excluded']+=in_period;continue
        if source.id in index.conflicts:
            coverage['identity_conflict']+=in_period;continue
        coverage['sources']+=in_period
        if not (source.summary or '').strip():coverage['empty']+=in_period
        dated=source.actor_type not in ('migration','import','system')
        coverage['recording_date_only']+=in_period and not dated
        audit=current_audit(source,audits[source.id])
        classification=audit_classification(session,source,audit)
        if classification is None:coverage['unclassified']+=in_period;continue
        policy_versions.add(audit.details['policy_version'] if audit else VERSION)
        if classification.get('business_rule_projection'):
            policy_versions.add(classification['business_rule_projection']['version'])
        units=classification.get('units',[{'unit':None,'classification':classification}])
        if not units:
            coverage['noncall']+=in_period
            continue
        for unit in units:
            values={k:v['value'] for k,v in unit['classification'].items()}
            if values['call'] is not True:
                coverage['call_unknown' if values['call'] is None else 'noncall']+=in_period
                continue
            literal=(unit['unit'] or {}).get('date_literal')
            if dated:
                day=source.occurred_at.astimezone(calendar).date()
                instant=source.occurred_at
            elif literal:
                try:day=date.fromisoformat(literal[:10])
                except ValueError:continue
                # Count by the literal calendar date only; don't fabricate an
                # offset or a midnight instant for an imported local timestamp.
                instant=None
            else:continue
            relevant=values['answered'] is True and values['role']=='decision_maker'
            weekly+=first_week<=day<last_week and relevant
            calls.append((source,values,index.companies[source.id],day,instant))
    history=defaultdict(list)
    for source,_,identity,day,instant in calls:history[identity].append((day,instant))
    for source,values,identity,day,instant in calls:
        if not first_day<=day<last_day:continue
        counts['calls']+=1
        counts['recovered_note_calls']+=source.activity_type=='note'
        counts['answered']+=values['answered'] is True
        counts['no_answer']+=values['answered'] is False
        counts['answer_unknown']+=values['answered'] is None
        counts['gatekeeper']+=values['answered'] is True and values['role']=='gatekeeper'
        counts['relevant']+=values['answered'] is True and values['role']=='decision_maker'
        counts['meetings']+=values['meeting']=='yes'
        counts['callbacks']+=values['callback']=='yes'
        dates=history[identity]
        earlier=any(d<day or (d==day and t is not None and instant is not None and t<instant) for d,t in dates)
        ambiguous=sum(d==day and (t is None or instant is None or t==instant) for d,t in dates)>1
        kind=('follow_up' if values['followup']=='yes' else 'first_recorded') if 'followup' in values else (
            'follow_up' if earlier else 'order_unknown' if ambiguous else 'first_recorded')
        counts[kind]+=1
    return {'policy_version':VERSION,'counts':counts,'coverage':coverage,'weekly_relevant':weekly,
        'policy_versions':sorted(policy_versions),
        'week_start':week_start.isoformat(),'week_end_exclusive':week_end.isoformat(),
        'generated_at':datetime.now(UTC).isoformat(),'history_scope':'recorded_calls_only_not_first_ever'}
