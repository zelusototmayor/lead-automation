"""Read-only company cohorts from dated CRM evidence; never classify source notes.

Jev/Sales assessment consumers publish into the existing canonical assessment
contract. This reader only accepts current effective values, including overrides.
"""
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from statistics import mean, median
from sqlalchemy import select
from src.crm.activity_provenance import operational_activity_filter
from src.crm.persistence.models import Account, Activity, Lead, Meeting, Proposal, SourceIdentity


def fold_companies(companies):
    rows = list(companies)
    touched = [r for r in rows if r['first_contact']]
    won = [r for r in touched if r['won']]
    closed = [r for r in touched if r['won'] or r['lost']]
    intervals = [(r['won_at']-r['first_contact']).total_seconds()/86400 for r in won
                 if r['won_at'] and r['won_at'] >= r['first_contact']]
    return {
        'companies': len(rows), 'contacted_companies': len(touched),
        'relevant_conversation_companies': sum(bool(r['relevant_calls']) for r in touched),
        'relevant_call_events': sum(r['relevant_calls'] for r in touched),
        'meeting_held_companies': sum(r['meeting_held'] for r in touched),
        'proposal_sent_companies': sum(r['proposal_sent'] for r in touched),
        'won_companies': len(won), 'lost_companies': sum(r['lost'] and not r['won'] for r in touched),
        'still_open_companies': len(touched)-len(closed),
        'observed_contact_to_win_rate': round(len(won)/len(touched),4) if touched else None,
        'closed_company_win_rate': round(len(won)/len(closed),4) if closed else None,
        'contacted_companies_per_win': round(len(touched)/len(won),2) if won else None,
        'relevant_call_events_per_win': round(sum(r['relevant_calls'] for r in touched)/len(won),2) if won else None,
        'observed_cycle_days': {'sample':len(intervals),'mean':round(mean(intervals),2) if intervals else None,
                                'median':round(median(intervals),2) if intervals else None},
        'won_without_usable_cycle':len(won)-len(intervals),
        'sample_warning': 'small_sample' if len(closed)<5 else None,
    }


def performance(session, workspace_id, *, now=None):
    now = now or datetime.now(UTC)
    accounts = {a.id:a for a in session.scalars(select(Account).where(Account.workspace_id==workspace_id))}
    leads = list(session.scalars(select(Lead).where(Lead.workspace_id==workspace_id)))
    def company_key(account_id, lead_id=None):
        seen=set()
        while account_id in accounts and accounts[account_id].merged_into_account_id:
            if account_id in seen: return ('unresolved',str(lead_id or account_id))
            seen.add(account_id);account_id=accounts[account_id].merged_into_account_id
        return ('account',str(account_id)) if account_id in accounts else ('lead',str(lead_id))
    records={}
    def get(key):
        if key not in records:
            records[key]=dict(first_contact=None, won_at=None, won=False,lost=False, stages=[],
                              relevant_calls=0,meeting_held=False,proposal_sent=False,
                              sector='unknown',origin='unknown',call_events=0, relevance_unknown=0)
        return records[key]
    lead_keys={}
    for lead in leads:
        key=company_key(lead.account_id,lead.id);lead_keys[lead.id]=key;r=get(key)
        r['stages'].append(lead.stage)
        account=accounts.get(lead.account_id)
        if account:
            r['sector']=account.sector or account.commercial_vertical or 'unknown'
            r['origin']=account.source_origin or 'unknown'
        r['won'] |= lead.stage=='won'
    sources={s.id:s for s in session.scalars(select(SourceIdentity).where(SourceIdentity.workspace_id==workspace_id))}
    events=list(session.scalars(select(Activity).where(Activity.workspace_id==workspace_id,
        Activity.occurred_at<=now,operational_activity_filter()).order_by(Activity.occurred_at,Activity.id)))
    calls=[e for e in events if e.activity_type=='call']
    index=None
    if calls:
        from src.crm.services.commercial_kpi_service import SourceIndex, effective
        index=SourceIndex(session,workspace_id)
    seen_messages=set();coverage=Counter();weekly=Counter()
    for event in events:
        key=lead_keys.get(event.lead_id) or company_key(event.account_id,event.lead_id)
        if key[1]=='None': coverage['unmapped_events']+=1;continue
        r=get(key);source=sources.get(event.source_identity_id)
        if source and source.source_system=='gmail' and source.entity_kind=='message':
            msg_key=(source.source_scope,source.external_id,event.activity_type)
            if msg_key in seen_messages:coverage['duplicate_message_events']+=1;continue
            seen_messages.add(msg_key)
        if event.activity_type=='call':
            ctx=index.context(event.id)
            value=effective(session,workspace_id,event.id,index=index).get('assessment') or {}
            if ctx.get('exclusion_reason') or value.get('freshness')=='current' and value.get('exclusion_reason'):
                coverage['excluded_calls']+=1;continue
            r['call_events']+=1
            if value.get('freshness')=='current' and value.get('relevant')=='yes':r['relevant_calls']+=1
            elif value.get('freshness')!='current' or value.get('relevant')=='unknown':r['relevance_unknown']+=1
        contact = event.activity_type in ('call','email_received','meeting') or (
            event.activity_type=='email_sent' and event.direction=='outbound')
        if contact:
            r['first_contact']=min(r['first_contact'] or event.occurred_at,event.occurred_at)
            if now-timedelta(days=7)<=event.occurred_at:weekly[event.activity_type]+=1
        if event.to_stage=='won':r['won_at']=min(r['won_at'] or event.occurred_at,event.occurred_at)
    for meeting in session.scalars(select(Meeting).where(Meeting.workspace_id==workspace_id,Meeting.status=='held')):
        if meeting.held_at and meeting.held_at<=now:get(company_key(meeting.account_id))['meeting_held']=True
    for proposal in session.scalars(select(Proposal).where(Proposal.workspace_id==workspace_id)):
        r=get(company_key(proposal.account_id,proposal.lead_id))
        if proposal.sent_at and proposal.sent_at<=now and proposal.status!='draft':r['proposal_sent']=True
        if proposal.status=='won':
            r['won']=True
            if proposal.won_at and proposal.won_at<=now:r['won_at']=min(r['won_at'] or proposal.won_at,proposal.won_at)
    cohorts=defaultdict(list);sectors=defaultdict(list);origins=defaultdict(list)
    for r in records.values():
        r['lost']=bool(r['stages']) and all(s in ('lost','not_a_fit') for s in r['stages']) and not r['won']
        month=r['first_contact'].strftime('%Y-%m') if r['first_contact'] else 'not_observed'
        cohorts[month].append(r);sectors[r['sector']].append(r);origins[r['origin']].append(r)
    return dict(schema_version=1,generated_at=now.isoformat(),scope='all_registered_history',
        company_identity='canonical_account_else_unlinked_lead',summary=fold_companies(records.values()),
        first_observed_contact_cohorts={k:fold_companies(v) for k,v in sorted(cohorts.items())},
        by_sector={k:fold_companies(v) for k,v in sorted(sectors.items())},
        by_origin={k:fold_companies(v) for k,v in sorted(origins.items())},
        last_7_days_recorded_interactions=dict(weekly),
        coverage=dict(coverage)|{'unlinked_leads':sum(k[0]!='account' for k in records),
            'call_events':sum(r['call_events'] for r in records.values()),
            'relevance_unknown_or_unprocessed':sum(r['relevance_unknown'] for r in records.values()),
            'companies_without_sector':len(sectors.get('unknown',[]))},
        limitations=[
            'First contact means earliest dated operational interaction recorded, not guaranteed first-ever contact.',
            'Conversions are observed company cohorts; still-open opportunities are not losses.',
            'Cycle times cover dated wins only; imported context dates and record update times are not close dates.',
            'Relevant calls use current canonical assessments; pending/stale/Jev-not-yet-published labels remain unknown.',
            'Sector/origin compare recorded attributes. Size, ICP fit and causal attribution are unavailable unless separately evidenced.',
            'Small closed samples cannot establish the best ICP or validate 50 conversations per win.',
            'No note reclassification or CRM mutation is performed.'])
