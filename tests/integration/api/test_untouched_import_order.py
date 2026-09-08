"""Exact import cohort ordering is read-only and precedes pagination."""
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from src.crm.persistence.models import Activity, Lead
from tests.integration.api.test_pipeline_api import pipeline_api
from tests.migration._postgres import require_disposable_postgres


def test_exact_cohort_order_precedes_pagination_without_changing_membership(pipeline_api):
    client, seed_id, _ = pipeline_api
    manifest = json.loads((Path(__file__).parents[2] / 'fixtures/transportes_20260907.json').read_text())
    records = manifest['records']
    assert len(records) == len({r['lead_id'] for r in records}) == 379
    ids = [UUID(r['lead_id']) for r in records]
    assert [r['source_row'] for r in records] == sorted({r['source_row'] for r in records})
    engine = create_engine(require_disposable_postgres())
    with Session(engine) as s, s.begin():
        workspace = s.get(Lead, seed_id).workspace_id
        now = datetime(2026, 9, 8, tzinfo=UTC)
        for i, id in enumerate(ids):
            # Imported later rows deliberately have newer timestamps.
            s.add(Lead(id=id, workspace_id=workspace, company_name=f'Import fixture {i}',
                       stage='contacted' if i == 1 else ('lost' if i == 2 else 'new'),
                       priority='low', updated_at=now + timedelta(seconds=i)))
        outsider = uuid4()
        s.add(Lead(id=outsider, workspace_id=workspace, company_name='Not imported',
                   stage='new', priority='high', updated_at=now + timedelta(days=1)))
        s.flush()
        s.add(Activity(workspace_id=workspace, lead_id=ids[3], activity_type='call',
                       title='Recorded real attempt', actor_type='human', occurred_at=now))
    expected_cohort = [str(id) for i,id in enumerate(ids) if i not in {1,2,3}]
    def fetch(queue, size):
        result=[]
        while True:
            response=client.get('/api/v1/pipeline/items',params={'queue':queue,'limit':size,'offset':len(result)})
            assert response.status_code == 200, response.text
            page=response.json(); result.extend(x['lead_id'] for x in page['items'])
            if len(result)>=page['total']:break
            assert page['items']
        assert len(result)==len(set(result))==page['total']
        return result
    with Session(engine) as s:
        before=[tuple(x) for x in s.execute(select(Lead.id,Lead.updated_at,Lead.version,Lead.priority,Lead.stage).where(Lead.workspace_id==workspace).order_by(Lead.id))]
    all_before=fetch('all',100)
    result=fetch('untouched',50)
    assert result[:len(expected_cohort)] == expected_cohort
    assert str(outsider) in result[len(expected_cohort):]
    assert not set(str(ids[i]) for i in {1,2,3}) & set(result)
    assert fetch('untouched',37)==result
    assert fetch('all',37)==all_before
    assert all_before[0]==str(outsider)
    assert result[len(expected_cohort):]==[id for id in all_before if id not in {str(x) for x in ids} and id in result]
    with Session(engine) as s:
        after=[tuple(x) for x in s.execute(select(Lead.id,Lead.updated_at,Lead.version,Lead.priority,Lead.stage).where(Lead.workspace_id==workspace).order_by(Lead.id))]
    assert before==after
    engine.dispose()
