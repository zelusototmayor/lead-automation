"""Bounded callback suggestions from explicit window and verified availability."""
from datetime import timedelta
from zoneinfo import ZoneInfo


def choose_slot(now, *, earliest, latest, busy, timezone='Europe/Lisbon',
                period='any', duration_minutes=10):
    if busy is None:
        raise ValueError('availability_unknown')
    if period not in ('morning', 'afternoon', 'any') or not 1 <= duration_minutes <= 60:
        raise ValueError('invalid_window')
    if any(t.tzinfo is None for t in (now, earliest, latest)):
        raise ValueError('timezone_required')
    if latest <= earliest or latest - earliest > timedelta(days=14):
        raise ValueError('invalid_window')
    if any(a.tzinfo is None or b.tzinfo is None or b <= a for a,b in busy):
        raise ValueError('availability_invalid')
    zone = ZoneInfo(timezone)
    cursor = max(now + timedelta(seconds=1), earliest).astimezone(zone)
    cursor = cursor.replace(second=0, microsecond=0) + timedelta(
        minutes=(15 - cursor.minute % 15) % 15)
    if cursor <= now or cursor < earliest:
        cursor += timedelta(minutes=15)
    duration = timedelta(minutes=duration_minutes)
    while cursor + duration <= latest:
        finish = cursor + duration
        morning = 9 <= cursor.hour < 12 and (finish.hour < 12 or finish.hour == 12 and finish.minute == 0)
        afternoon = 14 <= cursor.hour < 18 and (finish.hour < 18 or finish.hour == 18 and finish.minute == 0)
        if (cursor.weekday() < 5 and
                ((period in ('any','morning') and morning) or (period in ('any','afternoon') and afternoon))
                and not any(cursor < end and start < finish for start,end in busy)):
            return cursor
        cursor += timedelta(minutes=15)
    return None


def suggest_slot(session, workspace_id, day, period):
    from datetime import UTC, datetime, time
    from sqlalchemy import select
    from src.crm.persistence.models import Task
    from src.crm.services.callback_execution import calendar_from_environment
    calendar = calendar_from_environment()
    zone = ZoneInfo(calendar.timezone)
    now = datetime.now(UTC)
    start = datetime.combine(day, time.min, zone)
    end = start + timedelta(days=1)
    base = {'status':'availability_unknown','due_at':None,'timezone':zone.key,
            'provider_read_at':None,'policy':'weekday_09_12_14_18_suggestion_only',
            'duration_minutes':calendar.duration_minutes}
    if not now.astimezone(zone).date() <= day <= (now + timedelta(days=13)).astimezone(zone).date():
        raise ValueError('Choose a date within the next 14 days')
    if period not in ('morning','afternoon','any'):
        raise ValueError('Invalid period')
    if not calendar.configured():
        return base
    try:
        response=calendar._request('POST','/freeBusy',json={'timeMin':start.isoformat(),
            'timeMax':end.isoformat(),'timeZone':zone.key,'items':[{'id':calendar.calendar_id}]},timeout=5)
        response.raise_for_status()
        source=response.json()['calendars'][calendar.calendar_id]
        if source.get('errors') or not isinstance(source.get('busy'),list):
            return base
        busy=[(datetime.fromisoformat(item['start'].replace('Z','+00:00')),
               datetime.fromisoformat(item['end'].replace('Z','+00:00'))) for item in source['busy']]
        base['provider_read_at']=datetime.now(UTC).isoformat()
    except Exception:
        return base
    due_dates=session.scalars(select(Task.due_at).where(Task.workspace_id==workspace_id,
        Task.task_type=='call',Task.status=='open',Task.due_at >= start-timedelta(hours=1),Task.due_at<end))
    busy.extend((due,due+timedelta(minutes=calendar.duration_minutes)) for due in due_dates)
    try:
        slot=choose_slot(now,earliest=start,latest=end,busy=busy,timezone=zone.key,
                         period=period,duration_minutes=calendar.duration_minutes)
    except ValueError:
        return base
    return {**base,'status':'suggested' if slot else 'no_slot','due_at':slot.isoformat() if slot else None}
