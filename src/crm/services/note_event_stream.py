"""Read-only durable outbox stream over existing authenticated SSH.

LISTEN commits before snapshot, so overlapping notifications may duplicate IDs
but no commit is missed. Idle waits never scan tables. Only IDs leave the host.
"""
from __future__ import annotations
import argparse
import json
import os
import sys
from uuid import UUID


def replay(conn, workspace, emit):
    after = UUID(int=0)
    while True:
        rows = conn.execute("SELECT id FROM agent_work WHERE workspace_id=%s "
            "AND id>%s AND kind IN ('call_followup','calendar_callback') "
            "AND status IN ('queued','running') AND payload @> '{\"note_event_v1\":true}'::jsonb "
            "ORDER BY id LIMIT 200", (workspace, after)).fetchall()
        for (key,) in rows:
            emit({'workspace_id':str(workspace),'work_id':str(key)})
        if len(rows)<200:
            return
        after = rows[-1][0]


def stream(conn, workspace, emit, *, after_listen=None, timeout=None, stop_after_wait=False, heartbeat=None):
    # Caller must use autocommit: LISTEN is effective before the SELECT snapshot.
    conn.execute('LISTEN crm_note_work_v1')
    if after_listen:
        after_listen()
    replay(conn, workspace, emit)
    while True:
        for notification in conn.notifies(timeout=min(timeout if timeout is not None else 30, 30)):
            try:
                value=json.loads(notification.payload)
                if value.get('workspace_id') != str(workspace):
                    continue
                key=str(UUID(value['work_id']))
                if key!=value['work_id']:
                    continue
                emit({'workspace_id':str(workspace),'work_id':key})
            except (ValueError, KeyError, TypeError, AttributeError):
                continue
        if heartbeat:
            # A blank line is ignored by clients and detects disconnected SSH pipes.
            heartbeat()
        if stop_after_wait:
            return


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--once', action='store_true', help='bounded recovery snapshot only')
    args=parser.parse_args()
    import psycopg
    from sqlalchemy.engine import make_url
    # Reuse deployed runtime DSN and workspace: never pass credentials in argv.
    url=make_url(os.environ['DATABASE_URL'])
    workspace=UUID(os.environ['CRM_AUTOMATION_WORKSPACE_ID'])
    def emit(value):
        print(json.dumps(value, separators=(',',':')), flush=True)
    with psycopg.connect(host=url.host, port=url.port or 5432, user=url.username,
            password=url.password, dbname=url.database, autocommit=True,
            application_name='crm-note-events', **dict(url.query)) as conn:
        if args.once:
            replay(conn,workspace,emit)
        else:
            stream(conn,workspace,emit,heartbeat=lambda: print("", flush=True))


if __name__=='__main__':
    try:
        main()
    except BrokenPipeError:
        # Avoid a second failed flush during interpreter shutdown.
        sys.stdout = None
        sys.exit(0)
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception:
        # Don't leak DSN, raw note data or provider/DB errors to launchd logs.
        print('CRM note stream unavailable',file=sys.stderr)
        sys.exit(1)
