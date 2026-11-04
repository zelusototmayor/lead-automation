"""Append-only provider projection. The only provider write is POST (no invites)."""
from datetime import UTC, datetime, timedelta
from uuid import uuid5
from urllib.parse import quote
from zoneinfo import ZoneInfo


def sync_task(calendar, task, *, occurrence=None, legacy_event_id='', legacy_proof=None,
              company_name='', call_notes='', content_only=False):
    from src.crm.services.callback_execution import CalendarProjectionError
    if content_only:
        raise CalendarProjectionError('Calendar history is immutable; content repair disabled')
    # Closing an obligation changes CRM only. Do not even fetch, recreate or
    # delete old Calendar history (including human edits/cancellations).
    if task.status != 'open':
        return {'provider':'google_calendar','calendar_id':calendar.calendar_id,
                'event_id':(occurrence or {}).get('event_id', legacy_event_id),
                'status':'history_preserved','verified':True}
    if not calendar.configured():
        raise CalendarProjectionError('Calendar integration is not configured')
    start=task.due_at.astimezone(ZoneInfo(calendar.timezone))
    # Production always supplies the durable enqueue-time occurrence. This
    # deterministic schedule fallback supports direct legacy callers only.
    event_id=(occurrence or {}).get('event_id') or ('crm'+uuid5(task.id,
        'callback:'+task.due_at.astimezone(UTC).isoformat()).hex)
    if occurrence and occurrence.get('due_at') != task.due_at.astimezone(UTC).isoformat():
        raise CalendarProjectionError('Callback occurrence changed')
    company_name=(company_name or '').strip()
    if not company_name:raise CalendarProjectionError('Callback company is unavailable')
    owner={'crm_task_id':str(task.id),'crm_workspace_id':str(task.workspace_id)}
    payload={'summary':'call: '+company_name,
        'description':(call_notes or '').strip() or 'Sem notas de chamada registadas no CRM.',
        'start':{'dateTime':start.isoformat(),'timeZone':calendar.timezone},
        'end':{'dateTime':(start+timedelta(minutes=15)).isoformat(),'timeZone':calendar.timezone},
        'extendedProperties':{'private':dict(owner,pt_logistics_callback='1',
            crm_occurrence_id=event_id,crm_due_at=start.isoformat(),
            crm_end_at=(start+timedelta(minutes=15)).isoformat())}}
    collection=f'/calendars/{quote(calendar.calendar_id,safe="")}/events'
    path=collection+'/'+quote(event_id,safe='')
    existing=calendar._request('GET',path,timeout=20)
    if existing.status_code == 410:
        raise CalendarProjectionError('Calendar history was removed; no resurrection')
    if existing.status_code==404:
        if (occurrence or {}).get('projected'):
            raise CalendarProjectionError('Projected Calendar history missing; review required')
        response=calendar._request('POST',collection,json=dict(payload,id=event_id),
                                   params={'sendUpdates':'none'},timeout=20)
        if response.status_code!=409:response.raise_for_status()
    else:
        existing.raise_for_status()
    # Also verifies uncertain POST/409. Never repair or adopt somebody else's
    # edited event; preserve it and report the conflict for scoped review.
    readback=calendar._request('GET',path,timeout=20)
    readback.raise_for_status()
    observed=readback.json()
    calendar._assert_owner(observed,owner)
    if (observed.get('id')!=event_id or observed.get('status')=='cancelled'
            or observed.get('attendees') or observed.get('recurrence')
            or observed.get('recurringEventId')):
        raise CalendarProjectionError('Calendar occurrence conflict; history unchanged')
    try:
        matching=all(datetime.fromisoformat(observed[key]['dateTime'].replace('Z','+00:00'))
                     ==datetime.fromisoformat(payload[key]['dateTime']) for key in ('start','end'))
    except (KeyError,ValueError,TypeError):matching=False
    if not matching or any(observed.get(k)!=payload[k] for k in ('summary','description')):
        raise CalendarProjectionError('Calendar occurrence edited; history unchanged')
    return {'provider':'google_calendar','calendar_id':calendar.calendar_id,'event_id':event_id,
            'status':'scheduled','verified':True,'due_at':task.due_at.isoformat()}
