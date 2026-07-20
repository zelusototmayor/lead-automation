from __future__ import annotations

from datetime import UTC, datetime
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.crm.persistence.models import Account, Activity, Lead, Task, Workspace
from tests.migration._postgres import require_disposable_postgres


ROOT = Path(__file__).resolve().parents[3]
CONFIG = ROOT / "migrations/alembic.ini"
NOW = datetime(2026, 7, 20, 12, tzinfo=UTC)


@pytest.fixture(scope="module")
def engine():
    database_url = require_disposable_postgres()
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(CONFIG), "upgrade", "head"],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    value = create_engine(database_url)
    yield value
    value.dispose()


@pytest.fixture(autouse=True)
def clean_database(engine):
    with engine.begin() as connection:
        connection.execute(text("TRUNCATE tasks, activities, leads, accounts, workspaces CASCADE"))


def _account_and_lead(engine):
    with Session(engine) as session, session.begin():
        workspace = Workspace(slug=f"pipeline-{uuid4().hex}", name="Pipeline")
        session.add(workspace)
        session.flush()
        account = Account(
            workspace_id=workspace.id,
            display_name="Acme",
            normalized_name="acme",
        )
        session.add(account)
        session.flush()
        lead = Lead(workspace_id=workspace.id, account_id=account.id)
        session.add(lead)
        session.flush()
        return workspace.id, account.id, lead.id


def _task(workspace_id, account_id, **values):
    return Task(
        workspace_id=workspace_id,
        account_id=account_id,
        title="Next action",
        due_at=NOW,
        owner_user_id=uuid4(),
        status="open",
        **values,
    )


def test_call_task_can_target_a_lead(engine):
    workspace_id, account_id, lead_id = _account_and_lead(engine)

    with Session(engine) as session, session.begin():
        task = _task(
            workspace_id,
            account_id,
            lead_id=lead_id,
            task_type="call",
        )
        session.add(task)
        session.flush()

        assert task.lead_id == lead_id


@pytest.mark.parametrize("task_type", ["call", "email"])
def test_lead_specific_task_requires_lead_id(engine, task_type):
    workspace_id, account_id, _ = _account_and_lead(engine)

    with Session(engine) as session:
        session.add(_task(workspace_id, account_id, task_type=task_type))
        with pytest.raises(IntegrityError, match="ck_tasks_lead_context"):
            session.flush()
        session.rollback()


def test_lead_specific_task_rejects_lead_from_a_different_account(engine):
    workspace_id, account_id, _ = _account_and_lead(engine)
    with Session(engine) as session, session.begin():
        other_account = Account(
            workspace_id=workspace_id,
            display_name="Other",
            normalized_name="other",
        )
        session.add(other_account)
        session.flush()
        other_lead = Lead(workspace_id=workspace_id, account_id=other_account.id)
        session.add(other_lead)
        session.flush()
        other_lead_id = other_lead.id

    with Session(engine) as session:
        session.add(
            _task(
                workspace_id,
                account_id,
                lead_id=other_lead_id,
                task_type="call",
            )
        )
        with pytest.raises(IntegrityError, match="fk_tasks_workspace_account_lead"):
            session.flush()
        session.rollback()


def test_call_activity_persists_structured_outcome_code(engine):
    workspace_id, account_id, lead_id = _account_and_lead(engine)

    with Session(engine) as session, session.begin():
        activity = Activity(
            workspace_id=workspace_id,
            account_id=account_id,
            lead_id=lead_id,
            activity_type="call",
            occurred_at=NOW,
            title="Call logged",
            outcome_code="no_answer",
        )
        session.add(activity)
        session.flush()
        activity_id = activity.id

    with Session(engine) as session:
        persisted = session.get(Activity, activity_id)
        assert persisted is not None
        assert persisted.outcome_code == "no_answer"


def test_activity_rejects_blank_outcome_code(engine):
    workspace_id, account_id, lead_id = _account_and_lead(engine)

    with Session(engine) as session:
        session.add(
            Activity(
                workspace_id=workspace_id,
                account_id=account_id,
                lead_id=lead_id,
                activity_type="call",
                occurred_at=NOW,
                title="Call logged",
                outcome_code=" ",
            )
        )
        with pytest.raises(IntegrityError, match="ck_activities_outcome_code_nonblank"):
            session.flush()
        session.rollback()
