"""Authenticated historical source archive, with no write surface."""

import os
from pathlib import Path
from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session
from dashboard.app.db import create_database_engine
from dashboard.app.security import CRMPrincipal, require_crm_principal
from src.crm.persistence.models import SourceIdentity

router = APIRouter()
templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parents[1] / "templates")
)
Principal = Annotated[CRMPrincipal, Depends(require_crm_principal)]


@router.get("/arquivo", response_class=HTMLResponse)
def archive_page(request: Request, principal: Principal):
    return templates.TemplateResponse(
        request,
        "archive/index.html",
        {"request": request, "subject": principal.subject},
    )


@router.get("/api/v1/archive")
def archive_rows(
    principal: Principal,
    search: str = Query(default="", max_length=200),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    engine = create_database_engine()
    try:
        with Session(engine) as session:
            rows = []
            for identity in session.scalars(
                select(SourceIdentity).where(
                    SourceIdentity.workspace_id == principal.workspace_id,
                    SourceIdentity.source_system == "google_sheets",
                    SourceIdentity.entity_kind == "lead",
                )
            ):
                meta = identity.metadata_json
                raw = meta.get("legacy_row")
                if not raw:
                    continue
                if (
                    search
                    and search.casefold()
                    not in " ".join(str(v) for v in raw.values()).casefold()
                ):
                    continue
                reason = (
                    "Contacto protegido"
                    if meta.get("suppressed")
                    else (
                        "Proposta por confirmar"
                        if meta.get("legacy_email_date_ambiguity")
                        else "Histórico preservado"
                    )
                )
                if not raw.get("Company"):
                    reason = "Empresa por identificar"
                rows.append(
                    {
                        "row_number": meta.get("row_number") or meta.get("locator"),
                        "company": raw.get("Company"),
                        "contact": raw.get("Contact"),
                        "email": raw.get("Email"),
                        "stage": raw.get("Stage"),
                        "reason": reason,
                        "values": raw,
                    }
                )
            rows.sort(key=lambda x: x["row_number"] or 0)
            return {
                "items": rows[offset : offset + limit],
                "total": len(rows),
                "limit": limit,
                "offset": offset,
            }
    finally:
        engine.dispose()


@router.get("/api/v1/archive/workbook")
def archive_workbook(principal: Principal):
    path = Path(os.environ.get("CRM_ARCHIVE_FILE", "/app/archive/workbook.json"))
    if (
        os.environ.get("CRM_ARCHIVE_WORKSPACE_ID") != str(principal.workspace_id)
        or not path.is_file()
    ):
        raise HTTPException(404, "Archive unavailable")
    return FileResponse(
        path,
        media_type="application/json",
        filename="crm-historico-2026-09-05.json",
        headers={"Cache-Control": "no-store"},
    )
