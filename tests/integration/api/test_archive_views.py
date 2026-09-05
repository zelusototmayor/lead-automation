"""Historical archive authorization, tenant isolation and original-data contract."""
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.orm import Session

from dashboard.app import main as dashboard_main
from dashboard.app.config import get_principal_settings
from dashboard.app.routers import archive_views
from dashboard.app.security import CRMPrincipal, require_crm_principal
from src.crm.persistence.models import SourceIdentity, Workspace
from tests.migration._postgres import require_disposable_postgres


@pytest.fixture
def archive(monkeypatch):
    engine = create_engine(require_disposable_postgres())
    workspace_id, foreign_id = uuid4(), uuid4()
    rows = [
        (workspace_id, "google_sheets", "lead", 2, {"Company": "Alfa", "notes": "Original 2024/02/03", "Stage": "Email Sent"}),
        (workspace_id, "google_sheets", "lead", 9, {"Company": "Beta", "notes": "CALLBACK São Gens", "Email": "beta@example.test"}),
        (foreign_id, "google_sheets", "lead", 1, {"Company": "FOREIGN PRIVATE RECORD"}),
        (workspace_id, "gmail", "lead", 3, {"Company": "WRONG SOURCE"}),
        (workspace_id, "google_sheets", "account", 4, {"Company": "WRONG ENTITY"}),
    ]
    with Session(engine) as session, session.begin():
        session.add_all(Workspace(id=wid, slug=f"archive-{wid}", name="Archive fixture") for wid in (workspace_id, foreign_id))
        session.flush()
        for wid, source, kind, number, raw in rows:
            session.add(SourceIdentity(workspace_id=wid, source_system=source, entity_kind=kind,
                source_scope="archive-fixture", external_id=str(number),
                metadata_json={"row_number": number, "legacy_row": raw, "suppressed": number == 9}))
    principal = CRMPrincipal(workspace_id=workspace_id, subject="archive-reader", permissions=frozenset({"crm:read"}))
    overrides = dashboard_main.app.dependency_overrides.copy()
    dashboard_main.app.dependency_overrides[require_crm_principal] = lambda: principal
    monkeypatch.setattr(archive_views, "create_database_engine", lambda: engine)
    client = TestClient(dashboard_main.app)
    try:
        yield client, engine, principal, foreign_id
    finally:
        dashboard_main.app.dependency_overrides.clear()
        dashboard_main.app.dependency_overrides.update(overrides)
        with Session(engine) as session, session.begin():
            session.execute(delete(SourceIdentity).where(SourceIdentity.workspace_id.in_([workspace_id, foreign_id])))
            session.execute(delete(Workspace).where(Workspace.id.in_([workspace_id, foreign_id])))
        engine.dispose()


def test_archive_routes_fail_closed_before_reading_database_or_file(monkeypatch):
    def forbidden_database():
        raise AssertionError("Unauthenticated request touched the database")
    monkeypatch.setattr(archive_views, "create_database_engine", forbidden_database)
    monkeypatch.delenv("CRM_PRINCIPAL_USERNAME", raising=False)
    monkeypatch.delenv("CRM_PRINCIPAL_PASSWORD", raising=False)
    get_principal_settings.cache_clear()
    try:
        client = TestClient(dashboard_main.app)
        for path in ("/arquivo", "/api/v1/archive", "/api/v1/archive/workbook"):
            response = client.get(path)
            assert response.status_code in (401, 403)
            assert response.headers["cache-control"] == "no-store"
            assert "legacy_row" not in response.text
    finally:
        get_principal_settings.cache_clear()


def test_archive_tenant_and_source_scope_cannot_be_overridden_by_query(archive):
    client, engine, principal, foreign_id = archive
    response = client.get(f"/api/v1/archive?workspace_id={foreign_id}&source_system=gmail")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    data = response.json()
    assert data["total"] == 2
    assert [row["company"] for row in data["items"]] == ["Alfa", "Beta"]
    assert data["items"][0]["values"]["notes"] == "Original 2024/02/03"
    assert data["items"][0]["stage"] == "Email Sent"
    assert data["items"][1]["reason"] == "Contacto protegido"
    assert "FOREIGN PRIVATE RECORD" not in response.text
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(SourceIdentity).where(SourceIdentity.workspace_id == principal.workspace_id)) == 4


def test_archive_search_matches_original_notes_and_pagination_is_bounded(archive):
    client, *_ = archive
    page = client.get("/api/v1/archive?search=callback%20s%C3%A3o&limit=1").json()
    assert page["total"] == 1
    assert page["items"][0]["company"] == "Beta"
    second = client.get("/api/v1/archive?limit=1&offset=1").json()
    assert second["total"] == 2 and second["offset"] == 1
    assert [row["company"] for row in second["items"]] == ["Beta"]
    for query in ("limit=0", "limit=101", "offset=-1", "search=" + "x" * 201):
        assert client.get("/api/v1/archive?" + query).status_code == 422
    assert client.post("/api/v1/archive", json={"company": "Mutation"}).status_code == 405


def test_workbook_requires_exact_workspace_and_only_serves_configured_file(archive, monkeypatch, tmp_path):
    client, _, principal, foreign_id = archive
    workbook = tmp_path / "workbook.json"
    workbook.write_text('{"original":"preserved"}')
    other_file = tmp_path / "other.json"
    other_file.write_text('{"private":"unrelated"}')
    monkeypatch.setenv("CRM_ARCHIVE_FILE", str(workbook))
    monkeypatch.delenv("CRM_ARCHIVE_WORKSPACE_ID", raising=False)
    assert client.get("/api/v1/archive/workbook").status_code == 404
    monkeypatch.setenv("CRM_ARCHIVE_WORKSPACE_ID", str(foreign_id))
    assert client.get("/api/v1/archive/workbook").status_code == 404
    monkeypatch.setenv("CRM_ARCHIVE_WORKSPACE_ID", str(principal.workspace_id))
    response = client.get("/api/v1/archive/workbook", params={"path": str(other_file), "workspace_id": str(foreign_id)})
    assert response.status_code == 200
    assert response.json() == {"original": "preserved"}
    assert response.headers["cache-control"] == "no-store"
    assert "attachment" in response.headers["content-disposition"]
    workbook.unlink()
    assert client.get("/api/v1/archive/workbook").status_code == 404
