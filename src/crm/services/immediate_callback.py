"""Project exactly one committed human callback, independent of agent schedules."""
import logging
from uuid import UUID
from sqlalchemy import select
from src.crm.persistence.models import Task, AgentWork
from src.crm.services.agent_work_service import enqueue_callback, claim_work, fail_work, locked_work, finish_work
from src.crm.services.callback_execution import execute_callback

logger = logging.getLogger(__name__)


def sync_callback_now(session_factory, workspace_id, task_id, *, expected_version=None):
    if task_id is None:
        return "not_required"
    claimed = None
    work_id = None
    try:
        # No provider write before the original command and durable work commit.
        with session_factory() as session, session.begin():
            task = session.scalar(select(Task).where(Task.workspace_id == workspace_id,
                                                     Task.id == task_id).with_for_update())
            if expected_version is not None and (task is None or task.version != expected_version):
                return "superseded"
            if task is None:
                return "not_required"
            work_id = enqueue_callback(session, task)
            if work_id is None:
                return "not_required"
        with session_factory() as session, session.begin():
            items = claim_work(session, workspace_id, worker_id="manual-callback",
                               kinds=["calendar_callback"], work_ids=[work_id], limit=1)
            if not items:
                row = session.get(AgentWork, work_id)
                return "synced" if row.status == "completed" else "pending"
            claimed = items[0]
        with session_factory() as session, session.begin():
            # Match executor lock order; a concurrent human edit wins over this
            # version-bound recovery request, with no stale provider mutation.
            if expected_version is not None:
                locked_work(session, workspace_id, work_id)
                task = session.scalar(select(Task).where(Task.workspace_id == workspace_id,
                    Task.id == task_id).with_for_update())
                if task is None or task.version != expected_version:
                    finish_work(session, workspace_id, work_id, UUID(claimed["lease_token"]),
                                result={"summary": "Callback substituído por alteração mais recente", "evidence": []})
                    return "superseded"
            execute_callback(session, workspace_id, work_id, UUID(claimed["lease_token"]))
        return "synced"
    except Exception:
        # Saving the human action succeeded. Never turn a provider outage into
        # an HTTP failure which would encourage the user to log the call twice.
        logger.warning("Callback projection deferred; task=%s", task_id)
        if claimed:
            try:
                with session_factory() as session, session.begin():
                    fail_work(session, workspace_id, work_id, UUID(claimed["lease_token"]),
                              reason="Calendar projection failed; retry pending")
            except Exception:
                logger.warning("Callback lease recovery deferred; task=%s", task_id)
        return "pending"
