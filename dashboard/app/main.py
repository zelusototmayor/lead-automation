"""PT Logistics Dashboard - FastAPI application."""

from __future__ import annotations

import logging
import os
import sys
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.crm.pt_logistics_sheet import PTLogisticsCRM
from dashboard.app.feature_flags import get_feature_flags
from dashboard.app.db import create_database_engine
from dashboard.app.routers.accounts import router as accounts_router
from dashboard.app.routers.proposals import router as proposals_router
from dashboard.app.routers.intelligence import router as intelligence_router
from dashboard.app.routers.agent_events import router as agent_events_router
from dashboard.app.routers.operations import router as operations_router
from dashboard.app.routers.pipeline import router as pipeline_router
from dashboard.app.routers.tasks import router as tasks_router
from dashboard.app.routers.lead_commands import router as lead_commands_router
from dashboard.app.security import require_crm_principal, require_write_access

SPREADSHEET_ID = os.getenv(
    "SPREADSHEET_ID",
    "1ZdhkP_Hq-340eVEOS-RKwHGjDaX0vNVP6vO48XzkOx8",
)
CREDENTIALS_FILE = os.getenv(
    "GOOGLE_CREDENTIALS_FILE", "config/google_credentials.json"
)
SHEET_NAME = os.getenv("SHEET_NAME", "PT Logistics")
APP_TIMEZONE = os.getenv("APP_TIMEZONE", "Europe/Lisbon")
CALLBACK_CALENDAR_ID = os.getenv("CALLBACK_CALENDAR_ID", "")

crm: PTLogisticsCRM | None = None
logger = logging.getLogger(__name__)


def _unconfigured_shadow_comparison(_legacy_payload: dict[str, object]) -> None:
    raise RuntimeError("shadow comparison is not configured")


_account_shadow_comparison = _unconfigured_shadow_comparison
_proposal_shadow_comparison = _unconfigured_shadow_comparison


def _run_shadow_comparison(
    *, area: str, payload: dict[str, object], comparison
) -> None:
    try:
        comparison(payload)
    except Exception:
        logger.warning("%s shadow comparison failed", area)
    else:
        logger.info("%s shadow comparison completed", area)


def today_local() -> date:
    return datetime.now(ZoneInfo(APP_TIMEZONE)).date()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global crm
    flags = get_feature_flags()
    crm = None
    legacy_adapter_required = (
        flags.accounts_read_model != "postgres"
        or flags.proposals_read_model != "postgres"
        or flags.command_writer == "sheet"
        or flags.sheets_projection_enabled
    )
    if not legacy_adapter_required:
        yield
        return
    try:
        crm = PTLogisticsCRM(
            credentials_file=CREDENTIALS_FILE,
            spreadsheet_id=SPREADSHEET_ID,
            sheet_name=SHEET_NAME,
            callback_calendar_id=CALLBACK_CALENDAR_ID,
            app_timezone=APP_TIMEZONE,
        )
    except Exception as exc:
        print(f"Failed to initialize PT Logistics CRM: {exc}")
        crm = None
    yield


app = FastAPI(
    title="PT Logistics Dashboard",
    description="Call and follow-up dashboard for the PT Logistics CRM",
    lifespan=lifespan,
    openapi_url=None,
    docs_url=None,
    redoc_url=None,
)

# Temporary compatibility debt: the current Jinja/Alpine UI uses inline assets,
# and Alpine's standard CDN build evaluates expressions at runtime.
CONTENT_SECURITY_POLICY = "; ".join(
    (
        "default-src 'self'",
        "base-uri 'self'",
        "frame-ancestors 'none'",
        "object-src 'none'",
        "form-action 'self'",
        "img-src 'self' data:",
        "font-src 'self' https://fonts.gstatic.com",
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
        "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://cdn.jsdelivr.net",
        "connect-src 'self'",
    )
)


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/api/") and not (
        path == "/api/v1" or path.startswith("/api/v1/")
    ):
        response.headers["Deprecation"] = "true"
        route_template = getattr(request.scope.get("route"), "path", None)
        safe_path = (
            route_template
            if route_template and route_template.startswith("/api/")
            else "<unmatched>"
        )
        logger.info(
            "legacy API request path=%s status=%s", safe_path, response.status_code
        )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = (
        "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
    )
    response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
    if request.url.path.startswith(
        (
            "/api/",
            "/leads",
            "/contas",
            "/propostas",
            "/inteligencia",
            "/operacoes",
        )
    ):
        response.headers["Cache-Control"] = "no-store"
    return response


templates_dir = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(templates_dir))

static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    static_files = StaticFiles(directory=str(static_dir))

    @app.get(
        "/static/{path:path}",
        name="static",
        dependencies=[Depends(require_crm_principal)],
        include_in_schema=False,
    )
    async def protected_static(request: Request, path: str):
        return await static_files.get_response(path, request.scope)


app.include_router(accounts_router)
app.include_router(proposals_router)
app.include_router(intelligence_router)
app.include_router(agent_events_router)
app.include_router(operations_router)
app.include_router(pipeline_router)
app.include_router(tasks_router)
app.include_router(lead_commands_router)


@app.get("/up")
async def health_check():
    return JSONResponse({"status": "ok"})


def _postgres_is_ready() -> bool:
    engine = None
    try:
        engine = create_database_engine()
        with engine.connect() as connection:
            return connection.scalar(text("SELECT 1")) == 1
    except Exception:
        return False
    finally:
        if engine is not None:
            engine.dispose()


@app.get("/ready", dependencies=[Depends(require_crm_principal)])
async def readiness_check():
    try:
        flags = get_feature_flags()
    except ValueError:
        return JSONResponse(
            {"status": "not_ready", "dependencies": {}}, status_code=503
        )

    dependencies: dict[str, str] = {}
    legacy_required = (
        flags.accounts_read_model != "postgres"
        or flags.proposals_read_model != "postgres"
        or flags.command_writer == "sheet"
        or flags.sheets_projection_enabled
    )
    postgres_required = (
        flags.accounts_read_model != "legacy"
        or flags.proposals_read_model != "legacy"
        or flags.command_writer == "postgres"
        or flags.sheets_projection_enabled
        or flags.agent_events_enabled
    )
    if legacy_required:
        dependencies["legacy_sheet"] = "ready" if crm is not None else "unavailable"
    if postgres_required:
        dependencies["postgres"] = "ready" if _postgres_is_ready() else "unavailable"

    ready = all(state == "ready" for state in dependencies.values())
    return JSONResponse(
        {"status": "ready" if ready else "not_ready", "dependencies": dependencies},
        status_code=200 if ready else 503,
    )


@app.get(
    "/",
    response_class=HTMLResponse,
    dependencies=[Depends(require_crm_principal)],
)
async def dashboard(request: Request):
    if crm is None and get_feature_flags().accounts_read_model == "postgres":
        return RedirectResponse("/leads")
    return templates.TemplateResponse(
        request,
        "logistics.html",
        {
            "request": request,
            "sheet_name": SHEET_NAME,
            "today": today_local().isoformat(),
        },
    )


@app.get("/dashboard", dependencies=[Depends(require_crm_principal)])
async def dashboard_redirect():
    return RedirectResponse("/")


@app.get("/cold-calling", dependencies=[Depends(require_crm_principal)])
async def cold_calling_redirect():
    return RedirectResponse("/")


@app.get("/campaign/{slug}", dependencies=[Depends(require_crm_principal)])
async def campaign_redirect(slug: str):
    return RedirectResponse("/")


def _require_crm() -> PTLogisticsCRM | None:
    return crm


@app.get("/api/stats", dependencies=[Depends(require_crm_principal)])
async def api_stats():
    sheet = _require_crm()
    if not sheet:
        return JSONResponse(
            {"error": "PT Logistics CRM not initialized"}, status_code=503
        )
    try:
        return JSONResponse(sheet.get_stats(today_local()))
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/leads", dependencies=[Depends(require_crm_principal)])
async def api_leads(
    view: str = "today", q: str = "", priority: str = "", stage: str = ""
):
    sheet = _require_crm()
    if not sheet:
        return JSONResponse(
            {"error": "PT Logistics CRM not initialized"}, status_code=503
        )
    try:
        view = (
            view if view in {"today", "overdue", "due", "upcoming", "all"} else "today"
        )
        leads = (
            sheet.get_all_leads()
            if view == "all"
            else sheet.get_call_leads(view, today_local())
        )
        leads = _filter_leads(leads, q=q, priority=priority, stage=stage)
        return JSONResponse({"leads": leads, "count": len(leads), "view": view})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/email-followups", dependencies=[Depends(require_crm_principal)])
async def api_email_followups(
    view: str = "today",
    q: str = "",
    priority: str = "",
    stage: str = "",
    include_upcoming: bool = False,
):
    sheet = _require_crm()
    if not sheet:
        return JSONResponse(
            {"error": "PT Logistics CRM not initialized"}, status_code=503
        )
    try:
        view = (
            view if view in {"today", "overdue", "due", "upcoming", "all"} else "today"
        )
        tasks = sheet.get_outreach_followups(
            today_local(), view=view, include_upcoming=include_upcoming
        )
        tasks = _filter_leads(tasks, q=q, priority=priority, stage=stage)
        return JSONResponse({"tasks": tasks, "count": len(tasks), "view": view})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/outreach-followups", dependencies=[Depends(require_crm_principal)])
async def api_outreach_followups(
    view: str = "today",
    q: str = "",
    priority: str = "",
    stage: str = "",
    include_upcoming: bool = False,
):
    return await api_email_followups(
        view=view,
        q=q,
        priority=priority,
        stage=stage,
        include_upcoming=include_upcoming,
    )


@app.get("/api/proposal-followups", dependencies=[Depends(require_crm_principal)])
async def api_proposal_followups(
    view: str = "today",
    q: str = "",
    priority: str = "",
    stage: str = "",
    include_upcoming: bool = False,
):
    sheet = _require_crm()
    if not sheet:
        return JSONResponse(
            {"error": "PT Logistics CRM not initialized"}, status_code=503
        )
    try:
        view = (
            view if view in {"today", "overdue", "due", "upcoming", "all"} else "today"
        )
        tasks = sheet.get_proposal_followups(
            today_local(), view=view, include_upcoming=include_upcoming
        )
        tasks = _filter_leads(tasks, q=q, priority=priority, stage=stage)
        return JSONResponse({"tasks": tasks, "count": len(tasks), "view": view})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/proposals", dependencies=[Depends(require_crm_principal)])
async def api_proposals(
    view: str = "open", q: str = "", priority: str = "", stage: str = ""
):
    sheet = _require_crm()
    if not sheet:
        return JSONResponse(
            {"error": "PT Logistics CRM not initialized"}, status_code=503
        )
    try:
        view = view if view in {"open", "stale", "closed", "all"} else "open"
        leads = sheet.get_proposals(today_local(), view=view)
        leads = _filter_leads(leads, q=q, priority=priority, stage=stage)
        payload = {"leads": leads, "count": len(leads), "view": view}
        if get_feature_flags().proposals_read_model == "shadow":
            _run_shadow_comparison(
                area="proposal", payload=payload, comparison=_proposal_shadow_comparison
            )
        return JSONResponse(payload)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/impacted-leads", dependencies=[Depends(require_crm_principal)])
async def api_impacted_leads(q: str = "", priority: str = "", stage: str = ""):
    sheet = _require_crm()
    if not sheet:
        return JSONResponse(
            {"error": "PT Logistics CRM not initialized"}, status_code=503
        )
    try:
        leads = sheet.get_impacted_leads(today_local())
        leads = _filter_leads(leads, q=q, priority=priority, stage=stage)
        return JSONResponse({"leads": leads, "count": len(leads)})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/history", dependencies=[Depends(require_crm_principal)])
async def api_history(days: int = 30):
    sheet = _require_crm()
    if not sheet:
        return JSONResponse(
            {"error": "PT Logistics CRM not initialized"}, status_code=503
        )
    try:
        return JSONResponse(sheet.get_activity_history(today_local(), days=days))
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/account-profiles", dependencies=[Depends(require_crm_principal)])
async def api_account_profiles(stage: str = "Meeting Booked"):
    sheet = _require_crm()
    if not sheet:
        return JSONResponse(
            {"error": "PT Logistics CRM not initialized"}, status_code=503
        )
    try:
        payload = {"profiles": sheet.get_account_profiles(today_local(), stage=stage)}
        if get_feature_flags().accounts_read_model == "shadow":
            _run_shadow_comparison(
                area="account", payload=payload, comparison=_account_shadow_comparison
            )
        return JSONResponse(payload)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/portfolio", dependencies=[Depends(require_crm_principal)])
async def api_portfolio():
    sheet = _require_crm()
    if not sheet:
        return JSONResponse(
            {"error": "PT Logistics CRM not initialized"}, status_code=503
        )
    try:
        return JSONResponse(sheet.get_portfolio_summary(today_local()))
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/recommendations", dependencies=[Depends(require_crm_principal)])
async def api_recommendations():
    sheet = _require_crm()
    if not sheet:
        return JSONResponse(
            {"error": "PT Logistics CRM not initialized"}, status_code=503
        )
    try:
        return JSONResponse(
            {"recommendations": sheet.get_recommendations(today_local())}
        )
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/stage-timing", dependencies=[Depends(require_crm_principal)])
async def api_stage_timing(days: int = 120):
    sheet = _require_crm()
    if not sheet:
        return JSONResponse(
            {"error": "PT Logistics CRM not initialized"}, status_code=503
        )
    try:
        return JSONResponse(sheet.get_stage_timing(today_local(), days=days))
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.post("/api/log-call", dependencies=[Depends(require_write_access)])
async def api_log_call(request: Request):
    sheet = _require_crm()
    if not sheet:
        return JSONResponse(
            {"error": "PT Logistics CRM not initialized"}, status_code=503
        )
    try:
        body = await request.json()
        lead_id = body.get("lead_id", "")
        row_number = body.get("row_number", "")
        call_status = body.get("call_status", "")
        if (not lead_id and not row_number) or not call_status:
            return JSONResponse(
                {"error": "lead_id or row_number, plus call_status, required"},
                status_code=400,
            )

        ok = sheet.log_call(
            lead_id=lead_id,
            call_status=call_status,
            what_happened=body.get("what_happened", ""),
            notes=body.get("notes", ""),
            due=body.get("due", ""),
            due_time=body.get("due_time", ""),
            clear_due=bool(body.get("clear_due", False)),
            stage=body.get("stage", ""),
            row_number=row_number,
            touched_date=today_local(),
        )
        if not ok:
            return JSONResponse({"error": "Lead not found"}, status_code=404)
        return JSONResponse({"success": True, "warning": sheet.consume_warning()})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.post("/api/update-lead", dependencies=[Depends(require_write_access)])
async def api_update_lead(request: Request):
    sheet = _require_crm()
    if not sheet:
        return JSONResponse(
            {"error": "PT Logistics CRM not initialized"}, status_code=503
        )
    try:
        body = await request.json()
        lead_id = body.get("lead_id", "")
        row_number = body.get("row_number", "")
        updates = body.get("updates", {})
        if (not lead_id and not row_number) or not isinstance(updates, dict):
            return JSONResponse(
                {"error": "lead_id or row_number, plus updates, required"},
                status_code=400,
            )

        activity_type = (
            "email"
            if (updates.get("stage") or "").lower() == "email sent"
            else "update"
        )
        ok = sheet.update_lead(
            lead_id=lead_id,
            row_number=row_number,
            updates=updates,
            touched_date=today_local(),
            activity={
                "event_type": activity_type,
                "email_task": "Manual" if activity_type == "email" else "",
                "notes": "Quick update",
            },
        )
        if not ok:
            return JSONResponse({"error": "Lead not found"}, status_code=404)
        return JSONResponse({"success": True, "warning": sheet.consume_warning()})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.post("/api/mark-email-followup", dependencies=[Depends(require_write_access)])
async def api_mark_email_followup(request: Request):
    sheet = _require_crm()
    if not sheet:
        return JSONResponse(
            {"error": "PT Logistics CRM not initialized"}, status_code=503
        )
    try:
        body = await request.json()
        lead_id = body.get("lead_id", "")
        row_number = body.get("row_number", "")
        task_type = body.get("task_type", "")
        if (not lead_id and not row_number) or not task_type:
            return JSONResponse(
                {"error": "lead_id or row_number, plus task_type, required"},
                status_code=400,
            )

        ok = sheet.mark_email_followup_sent(
            lead_id=lead_id,
            task_type=task_type,
            sent_date=today_local(),
            notes=body.get("notes", ""),
            row_number=row_number,
            touched_date=today_local(),
        )
        if not ok:
            return JSONResponse(
                {"error": "Lead or follow-up task not found"}, status_code=404
            )
        return JSONResponse({"success": True})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.post("/api/mark-proposal-followup", dependencies=[Depends(require_write_access)])
async def api_mark_proposal_followup(request: Request):
    sheet = _require_crm()
    if not sheet:
        return JSONResponse(
            {"error": "PT Logistics CRM not initialized"}, status_code=503
        )
    try:
        body = await request.json()
        lead_id = body.get("lead_id", "")
        row_number = body.get("row_number", "")
        task_type = body.get("task_type", "")
        if (not lead_id and not row_number) or not task_type:
            return JSONResponse(
                {"error": "lead_id or row_number, plus task_type, required"},
                status_code=400,
            )

        ok = sheet.mark_proposal_followup_sent(
            lead_id=lead_id,
            task_type=task_type,
            sent_date=today_local(),
            notes=body.get("notes", ""),
            row_number=row_number,
            touched_date=today_local(),
        )
        if not ok:
            return JSONResponse(
                {"error": "Lead or proposal follow-up task not found"}, status_code=404
            )
        return JSONResponse({"success": True})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.post("/api/update-proposal", dependencies=[Depends(require_write_access)])
async def api_update_proposal(request: Request):
    sheet = _require_crm()
    if not sheet:
        return JSONResponse(
            {"error": "PT Logistics CRM not initialized"}, status_code=503
        )
    try:
        body = await request.json()
        lead_id = body.get("lead_id", "")
        row_number = body.get("row_number", "")
        if not lead_id and not row_number:
            return JSONResponse(
                {"error": "lead_id or row_number required"}, status_code=400
            )

        ok = sheet.update_proposal(
            lead_id=lead_id,
            row_number=row_number,
            status=body.get("status", ""),
            next_action=body.get("next_action") if "next_action" in body else None,
            next_action_due=body.get("next_action_due")
            if "next_action_due" in body
            else None,
            outcome=body.get("outcome", ""),
            lost_reason=body.get("lost_reason", ""),
            value=body.get("value", ""),
            probability=body.get("probability", ""),
            forecast_category=body.get("forecast_category", ""),
            notes=body.get("notes", ""),
            touched_date=today_local(),
        )
        if not ok:
            return JSONResponse({"error": "Lead not found"}, status_code=404)
        return JSONResponse({"success": True})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.post("/api/mark-email-sent", dependencies=[Depends(require_write_access)])
async def api_mark_email_sent(request: Request):
    sheet = _require_crm()
    if not sheet:
        return JSONResponse(
            {"error": "PT Logistics CRM not initialized"}, status_code=503
        )
    try:
        body = await request.json()
        lead_id = body.get("lead_id", "")
        row_number = body.get("row_number", "")
        if not lead_id and not row_number:
            return JSONResponse(
                {"error": "lead_id or row_number required"}, status_code=400
            )

        ok = sheet.mark_manual_email_sent(
            lead_id=lead_id,
            row_number=row_number,
            sent_date=today_local(),
            notes=body.get("notes", ""),
            touched_date=today_local(),
        )
        if not ok:
            return JSONResponse({"error": "Lead not found"}, status_code=404)
        return JSONResponse({"success": True})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.post("/api/refresh", dependencies=[Depends(require_write_access)])
async def api_refresh():
    sheet = _require_crm()
    if not sheet:
        return JSONResponse(
            {"error": "PT Logistics CRM not initialized"}, status_code=503
        )
    try:
        sheet._refresh_cache()
        return JSONResponse({"success": True, "rows": len(sheet._cache)})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


def _filter_leads(
    leads: list[dict], q: str = "", priority: str = "", stage: str = ""
) -> list[dict]:
    if q:
        needle = q.strip().lower()
        leads = [
            lead
            for lead in leads
            if needle
            in " ".join(
                [
                    lead.get("company", ""),
                    lead.get("contact", ""),
                    lead.get("phone", ""),
                    lead.get("email", ""),
                    lead.get("city", ""),
                    lead.get("region", ""),
                    lead.get("dashboard_touched", ""),
                ]
            ).lower()
        ]

    if priority:
        priority_lower = priority.lower()
        leads = [
            lead
            for lead in leads
            if (lead.get("priority") or "").lower() == priority_lower
        ]

    if stage:
        stage_lower = stage.lower()
        leads = [
            lead
            for lead in leads
            if ((lead.get("stage") or "").strip() or "Blank").lower() == stage_lower
        ]

    return leads
