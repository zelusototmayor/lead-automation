"""Lead Automation Dashboard - FastAPI Application"""

import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# Add parent directory to path so we can import from src
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.crm.sheets import GoogleSheetsCRM, CRM_HEADERS, COL
from src.crm.local_services_sheet import LocalServicesCRM

# Import directly to avoid loading personalize.py which requires anthropic
from src.outreach.instantly_client import InstantlyClient
from src.outreach.sync_instantly import InstantlySyncer

from .auth import authenticate
from .metrics import calculate_metrics, normalize_rows, merge_trends

# Instantly API key for sync
INSTANTLY_API_KEY = os.getenv("INSTANTLY_API_KEY")

# Configuration
SPREADSHEET_ID = os.getenv(
    "SPREADSHEET_ID",
    "1ZdhkP_Hq-340eVEOS-RKwHGjDaX0vNVP6vO48XzkOx8"
)
CREDENTIALS_FILE = os.getenv(
    "GOOGLE_CREDENTIALS_FILE",
    "config/google_credentials.json"
)

# Campaign registry
CAMPAIGNS = {
    "aec": {
        "name": "AEC",
        "sheet_name": os.getenv("SHEET_NAME", "AEC Leads"),
        "type": "email",
        "description": "Architecture, Engineering & Construction",
    },
    "b2b-startups": {
        "name": "B2B Startups",
        "sheet_name": os.getenv("B2B_SHEET_NAME", "B2B Startups"),
        "type": "email",
        "description": "B2B SaaS companies, 5-50 employees",
    },
    "local-services": {
        "name": "Local Services",
        "sheet_name": os.getenv("LS_SHEET_NAME", "Local Services"),
        "type": "phone",
        "description": "Staffing, logistics, real estate",
    },
    "eu-b2b": {
        "name": "EU B2B",
        "sheet_name": os.getenv("EU_SHEET_NAME", "EU B2B Leads"),
        "type": "email",
        "description": "EU B2B companies hiring SDR/BDR",
    },
}

# Global CRM instances
crm_instances: dict[str, GoogleSheetsCRM | LocalServicesCRM] = {}
crm: GoogleSheetsCRM | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize CRM instances on startup."""
    global crm

    for slug, cfg in CAMPAIGNS.items():
        try:
            if cfg["type"] == "phone":
                instance = LocalServicesCRM(
                    credentials_file=CREDENTIALS_FILE,
                    spreadsheet_id=SPREADSHEET_ID,
                    sheet_name=cfg["sheet_name"],
                )
                crm_instances[slug] = instance
                print(f"CRM initialized [{slug}]: {cfg['sheet_name']}")
            else:
                instance = GoogleSheetsCRM(
                    credentials_file=CREDENTIALS_FILE,
                    spreadsheet_id=SPREADSHEET_ID,
                    sheet_name=cfg["sheet_name"],
                )
                crm_instances[slug] = instance
                if slug == "aec":
                    crm = instance
                print(f"CRM initialized [{slug}]: {cfg['sheet_name']}")
        except Exception as e:
            print(f"Failed to initialize CRM [{slug}]: {e}")

    yield


app = FastAPI(
    title="Lead Automation Dashboard",
    description="Monitor lead generation performance",
    lifespan=lifespan,
)

# Templates
templates_dir = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(templates_dir))

# Static files (if any)
static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/up")
async def health_check():
    """Health check endpoint for Traefik."""
    return JSONResponse({"status": "ok"})


# ── Overview (home) ──────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def overview(request: Request):
    """Cross-campaign overview page."""
    campaigns_data = []
    all_trends = []

    for slug, cfg in CAMPAIGNS.items():
        instance = crm_instances.get(slug)
        if not instance:
            continue
        summary = _get_campaign_summary(slug, instance, cfg)
        campaigns_data.append(summary)
        if summary.get("trend"):
            all_trends.append(summary["trend"])

    merged_trend = merge_trends(all_trends) if all_trends else {"labels": [], "sent": [], "replies": []}

    total_leads = sum(c.get("total_leads", 0) for c in campaigns_data)
    total_contacted = sum(c.get("contacted", 0) for c in campaigns_data)
    total_replied = sum(c.get("replied", 0) for c in campaigns_data)
    total_won = sum(c.get("won", 0) for c in campaigns_data)
    reply_rate = round(total_replied / total_contacted * 100, 1) if total_contacted > 0 else 0
    conv_rate = round(total_won / total_leads * 100, 1) if total_leads > 0 else 0

    return templates.TemplateResponse(
        "overview.html",
        {
            "request": request,
            "campaigns": campaigns_data,
            "total_leads": total_leads,
            "total_contacted": total_contacted,
            "total_replied": total_replied,
            "total_won": total_won,
            "reply_rate": reply_rate,
            "conv_rate": conv_rate,
            "merged_trend": merged_trend,
        },
    )


# ── Campaign Detail ──────────────────────────────────────────────────

@app.get("/campaign/{slug}", response_class=HTMLResponse)
async def campaign_detail(request: Request, slug: str):
    """Per-campaign deep dive page."""
    if slug not in CAMPAIGNS:
        return RedirectResponse("/")

    cfg = CAMPAIGNS[slug]

    # For phone campaigns, render cold calling with campaign context
    if cfg["type"] == "phone":
        phone_campaigns = [
            {"slug": s, "name": c["name"], "active": s == slug}
            for s, c in CAMPAIGNS.items()
            if c["type"] == "phone"
        ]
        return templates.TemplateResponse(
            "cold_calling.html",
            {
                "request": request,
                "campaign_slug": slug,
                "campaign": cfg,
                "phone_campaigns": phone_campaigns,
            },
        )

    instance = crm_instances.get(slug)
    metrics = {}
    error = None
    leads_view = []
    all_rows = []

    if instance and isinstance(instance, GoogleSheetsCRM):
        try:
            all_values = instance.sheet.get_all_values()
            # Use the actual sheet header for column mapping — each sheet
            # may have columns in different order than CRM_HEADERS.
            header = all_values[0] if all_values else CRM_HEADERS
            all_rows = all_values[1:] if len(all_values) > 1 else []
            metrics = calculate_metrics(all_rows, header)
            leads_view = normalize_rows(all_rows, header)
        except Exception as e:
            error = f"Failed to load data: {e}"
    else:
        error = f"CRM not initialized for {cfg['name']}"

    # Get all email campaign slugs for the campaign selector
    email_campaigns = [
        {"slug": s, "name": c["name"], "active": s == slug}
        for s, c in CAMPAIGNS.items()
        if c["type"] == "email"
    ]

    return templates.TemplateResponse(
        "campaign_detail.html",
        {
            "request": request,
            "slug": slug,
            "campaign": cfg,
            "email_campaigns": email_campaigns,
            "metrics": metrics,
            "error": error,
            "leads_raw": all_rows,
            "leads_view": leads_view,
        },
    )


# ── Old dashboard redirect ──────────────────────────────────────────

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_redirect():
    """Redirect old dashboard URL to AEC campaign."""
    return RedirectResponse("/campaign/aec")


# ── Auth ─────────────────────────────────────────────────────────────

@app.post("/api/login")
async def login(request: Request):
    """Verify login credentials."""
    try:
        body = await request.json()
        username = body.get("username", "")
        password = body.get("password", "")

        correct = {
            "username": os.getenv("DASHBOARD_USER", "admin"),
            "password": os.getenv("DASHBOARD_PASSWORD", "changeme"),
        }

        import secrets
        username_ok = secrets.compare_digest(username, correct["username"])
        password_ok = secrets.compare_digest(password, correct["password"])

        if username_ok and password_ok:
            return JSONResponse({"success": True, "username": username})
        else:
            return JSONResponse({"success": False, "error": "Invalid credentials"}, status_code=401)
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=400)


# ── Campaign APIs ────────────────────────────────────────────────────

@app.get("/api/campaigns")
async def api_campaigns(username: str = Depends(authenticate)):
    """List all campaigns with summary stats."""
    results = []
    for slug, cfg in CAMPAIGNS.items():
        instance = crm_instances.get(slug)
        if not instance:
            results.append({"slug": slug, "name": cfg["name"], "type": cfg["type"], "error": "not initialized"})
            continue
        summary = _get_campaign_summary(slug, instance, cfg)
        results.append(summary)
    return JSONResponse(results)


@app.get("/api/campaign/{slug}/metrics")
async def api_campaign_metrics(slug: str, username: str = Depends(authenticate)):
    """Full metrics for a single email campaign."""
    if slug not in CAMPAIGNS:
        return JSONResponse({"error": "Campaign not found"}, status_code=404)

    cfg = CAMPAIGNS[slug]
    instance = crm_instances.get(slug)

    if not instance:
        return JSONResponse({"error": "CRM not initialized"}, status_code=503)

    if cfg["type"] == "phone":
        return JSONResponse({"error": "Use /api/cold-calling/stats for phone campaigns"}, status_code=400)

    try:
        all_values = instance.sheet.get_all_values()
        header = all_values[0] if all_values else CRM_HEADERS
        all_rows = all_values[1:] if len(all_values) > 1 else []
        metrics = calculate_metrics(all_rows, header)
        return JSONResponse(metrics)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/metrics")
async def api_metrics(username: str = Depends(authenticate)):
    """API endpoint for metrics (for AJAX refresh). Uses AEC by default."""
    if not crm:
        return JSONResponse({"error": "CRM not initialized"}, status_code=503)

    try:
        all_values = crm.sheet.get_all_values()
        header = all_values[0] if all_values else CRM_HEADERS
        all_rows = all_values[1:] if len(all_values) > 1 else []
        metrics = calculate_metrics(all_rows, header)
        return JSONResponse(metrics)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


import threading
import structlog

_sync_logger = structlog.get_logger("sync")
_sync_status = {"running": False, "last_result": None, "analytics": {}}


def _run_sync_background():
    """Run full Instantly sync in background thread, then fetch analytics."""
    total_results = {
        "campaigns_checked": 0, "leads_checked": 0, "crm_updated": 0,
        "replies_found": 0, "emails_fetched": 0, "not_in_crm": 0,
        "errors": []
    }
    try:
        instantly = None
        for slug, cfg in CAMPAIGNS.items():
            if cfg["type"] != "email":
                continue
            instance = crm_instances.get(slug)
            if not instance or not isinstance(instance, GoogleSheetsCRM):
                continue
            try:
                syncer = InstantlySyncer(INSTANTLY_API_KEY, instance)
                if instantly is None:
                    instantly = syncer.instantly
                results = syncer.sync_all_leads()
                for k in ("campaigns_checked", "leads_checked", "crm_updated",
                           "replies_found", "emails_fetched", "not_in_crm"):
                    total_results[k] += results.get(k, 0)
                total_results["errors"].extend(results.get("errors", []))
            except Exception as e:
                total_results["errors"].append(f"Error syncing {slug}: {str(e)}")

        # Fetch campaign analytics (single call for all campaigns)
        if instantly:
            try:
                analytics = instantly.get_campaign_analytics()
                if analytics:
                    _sync_status["analytics"] = analytics
                    _sync_logger.info("Campaign analytics cached")
            except Exception as e:
                _sync_logger.warning("Failed to fetch analytics", error=str(e))

        _sync_logger.info("Background sync complete", **total_results)
    finally:
        _sync_status["last_result"] = total_results
        _sync_status["running"] = False


@app.post("/api/sync-replies")
async def sync_replies(username: str = Depends(authenticate)):
    """Full sync from Instantly to all email CRMs (runs in background)."""
    if not INSTANTLY_API_KEY:
        return JSONResponse({"error": "INSTANTLY_API_KEY not configured"}, status_code=503)

    if _sync_status["running"]:
        return JSONResponse({"status": "already_running"})

    _sync_status["running"] = True
    _sync_status["last_result"] = None
    threading.Thread(target=_run_sync_background, daemon=True).start()

    return JSONResponse({"status": "started"})


@app.get("/api/sync-status")
async def sync_status(username: str = Depends(authenticate)):
    """Check sync status."""
    return JSONResponse({
        "running": _sync_status["running"],
        "last_result": _sync_status["last_result"],
    })


@app.get("/api/campaign-analytics")
async def campaign_analytics(username: str = Depends(authenticate)):
    """Return cached Instantly campaign analytics."""
    analytics = _sync_status.get("analytics")
    if not analytics:
        return JSONResponse({"error": "No analytics cached. Trigger a sync first."}, status_code=404)
    return JSONResponse(analytics)


# ── Cold Calling CRM Routes ──────────────────────────────────────────

@app.get("/cold-calling", response_class=HTMLResponse)
async def cold_calling_page(request: Request):
    """Serve cold calling CRM page — defaults to first phone campaign."""
    first_phone = next((s for s, c in CAMPAIGNS.items() if c["type"] == "phone"), None)
    if first_phone:
        return RedirectResponse(f"/campaign/{first_phone}")
    return templates.TemplateResponse("cold_calling.html", {
        "request": request,
        "campaign_slug": "",
        "campaign": {"name": "Cold Calling"},
        "phone_campaigns": [],
    })


def _get_phone_crm(campaign: str = "") -> LocalServicesCRM | None:
    """Resolve a phone campaign slug to its CRM instance."""
    if campaign and campaign in crm_instances:
        inst = crm_instances[campaign]
        if isinstance(inst, LocalServicesCRM):
            return inst
    # Fall back to first available phone CRM
    for slug, cfg in CAMPAIGNS.items():
        if cfg["type"] == "phone" and slug in crm_instances:
            return crm_instances[slug]
    return None


@app.get("/api/cold-calling/leads")
async def cold_calling_leads(
    view: str = "queue",
    vertical: str = "",
    city: str = "",
    status: str = "",
    campaign: str = "",
    username: str = Depends(authenticate),
):
    """Get leads for cold calling. view=queue returns call queue, view=all returns everything."""
    phone_crm = _get_phone_crm(campaign)
    if not phone_crm:
        return JSONResponse({"error": "Phone CRM not initialized"}, status_code=503)

    try:
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")

        if view == "queue":
            leads = phone_crm.get_call_queue(today)
        else:
            leads = phone_crm.get_all_leads()

        # Apply filters
        if vertical:
            v_lower = vertical.lower()
            leads = [l for l in leads if (l.get("vertical") or "").lower() == v_lower]
        if city:
            c_lower = city.lower()
            leads = [l for l in leads if (l.get("city") or "").lower() == c_lower]
        if status:
            s_lower = status.lower()
            leads = [l for l in leads if (l.get("status") or "").lower() == s_lower]

        return JSONResponse({"leads": leads, "count": len(leads)})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/cold-calling/stats")
async def cold_calling_stats(campaign: str = "", username: str = Depends(authenticate)):
    """Pipeline stats for KPI strip."""
    phone_crm = _get_phone_crm(campaign)
    if not phone_crm:
        return JSONResponse({"error": "Phone CRM not initialized"}, status_code=503)
    try:
        stats = phone_crm.get_pipeline_stats()
        return JSONResponse(stats)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/cold-calling/log-call")
async def cold_calling_log_call(request: Request, username: str = Depends(authenticate)):
    """Log a call outcome with notes and optional follow-up."""
    try:
        body = await request.json()
        campaign_slug = body.get("campaign", "")
        phone_crm = _get_phone_crm(campaign_slug)
        if not phone_crm:
            return JSONResponse({"error": "Phone CRM not initialized"}, status_code=503)

        lead_id = body.get("lead_id")
        call_status = body.get("call_status", "")
        notes = body.get("notes", "")
        followup_date = body.get("followup_date", "")
        new_status = body.get("new_status", "")

        if not lead_id or not call_status:
            return JSONResponse({"error": "lead_id and call_status required"}, status_code=400)

        ok = phone_crm.log_call(lead_id, call_status, notes, followup_date, new_status)
        if ok:
            return JSONResponse({"success": True})
        else:
            return JSONResponse({"error": "Lead not found"}, status_code=404)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/cold-calling/update-status")
async def cold_calling_update_status(request: Request, username: str = Depends(authenticate)):
    """Quick status change for a lead."""
    try:
        body = await request.json()
        campaign_slug = body.get("campaign", "")
        phone_crm = _get_phone_crm(campaign_slug)
        if not phone_crm:
            return JSONResponse({"error": "Phone CRM not initialized"}, status_code=503)

        lead_id = body.get("lead_id")
        new_status = body.get("status")

        if not lead_id or not new_status:
            return JSONResponse({"error": "lead_id and status required"}, status_code=400)

        ok = phone_crm.update_lead(lead_id, {"status": new_status})
        if ok:
            return JSONResponse({"success": True})
        else:
            return JSONResponse({"error": "Lead not found"}, status_code=404)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/cold-calling/refresh")
async def cold_calling_refresh(campaign: str = "", username: str = Depends(authenticate)):
    """Force cache refresh from Google Sheets."""
    phone_crm = _get_phone_crm(campaign)
    if not phone_crm:
        return JSONResponse({"error": "Phone CRM not initialized"}, status_code=503)
    try:
        phone_crm._refresh_cache()
        return JSONResponse({"success": True, "rows": len(phone_crm._cache)})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ── Debug ──────────────────────────────────────────────────────────────

@app.get("/api/debug/{slug}")
async def debug_campaign(slug: str):
    """Debug endpoint: shows raw sheet header vs CRM_HEADERS and sample parsed rows."""
    if slug not in CAMPAIGNS:
        return JSONResponse({"error": "not found"}, status_code=404)
    cfg = CAMPAIGNS[slug]
    instance = crm_instances.get(slug)
    if not instance or not isinstance(instance, GoogleSheetsCRM):
        return JSONResponse({"error": "not initialized"}, status_code=503)

    all_values = instance.sheet.get_all_values()
    sheet_header = all_values[0] if all_values else []
    rows = all_values[1:] if len(all_values) > 1 else []

    # Compare headers
    header_comparison = []
    max_len = max(len(sheet_header), len(CRM_HEADERS))
    for i in range(max_len):
        sh = sheet_header[i] if i < len(sheet_header) else "(missing)"
        ch = CRM_HEADERS[i] if i < len(CRM_HEADERS) else "(missing)"
        match = sh.strip().lower() == ch.strip().lower()
        header_comparison.append({"col": i, "sheet": sh, "crm_headers": ch, "match": match})

    # Parse first 3 rows with actual sheet header and show raw + parsed
    samples = []
    for row in rows[:3]:
        parsed = normalize_rows([row], sheet_header)[0]
        samples.append({
            "raw_row": row,
            "parsed": parsed,
        })

    return JSONResponse({
        "slug": slug,
        "sheet_header_len": len(sheet_header),
        "crm_headers_len": len(CRM_HEADERS),
        "header_comparison": header_comparison,
        "total_rows": len(rows),
        "samples": samples,
    })


# ── Helpers ───────────────────────────────────────────────────────────

def _get_campaign_summary(slug: str, instance, cfg: dict) -> dict:
    """Build a summary dict for a campaign."""
    from datetime import date, datetime, timedelta

    summary = {
        "slug": slug,
        "name": cfg["name"],
        "type": cfg["type"],
        "description": cfg.get("description", ""),
        "total_leads": 0,
        "contacted": 0,
        "replied": 0,
        "reply_rate": 0,
        "won": 0,
        "leads_this_week": 0,
        "trend": None,
    }

    try:
        if cfg["type"] == "phone":
            # LocalServicesCRM
            stats = instance.get_pipeline_stats()
            summary["total_leads"] = stats.get("total", 0)
            by_status = stats.get("by_status") or {}
            # Count all non-New statuses as contacted
            contacted = sum(v for k, v in by_status.items() if k.lower() != "new")
            summary["contacted"] = contacted
            meetings = by_status.get("Meeting Booked", 0)
            interested = by_status.get("Interested", 0)
            summary["replied"] = interested + meetings
            summary["won"] = meetings
            summary["reply_rate"] = round(summary["replied"] / summary["total_leads"] * 100, 1) if summary["total_leads"] > 0 else 0
        else:
            # GoogleSheetsCRM
            all_values = instance.sheet.get_all_values()
            header = all_values[0] if all_values else CRM_HEADERS
            rows = all_values[1:] if len(all_values) > 1 else []
            metrics = calculate_metrics(rows, header)

            summary["total_leads"] = metrics["summary"]["total_leads"]
            summary["contacted"] = metrics["summary"]["contacted"]
            summary["replied"] = metrics["summary"]["total_responses"]
            summary["reply_rate"] = metrics["summary"]["response_rate"]
            summary["won"] = metrics["pipeline"]["won"]
            summary["leads_this_week"] = _count_leads_this_week(rows, header)
            summary["trend"] = metrics.get("trend")

            # Override with Instantly analytics if cached (more accurate)
            analytics = _sync_status.get("analytics")
            if analytics and isinstance(analytics, dict):
                # Analytics may be a single dict or list of campaign dicts
                campaign_analytics = None
                if isinstance(analytics, list):
                    for a in analytics:
                        if a.get("campaign_name") == cfg["name"]:
                            campaign_analytics = a
                            break
                elif analytics.get("campaign_name") == cfg["name"]:
                    campaign_analytics = analytics

                if campaign_analytics:
                    if "contacted_count" in campaign_analytics:
                        summary["contacted"] = campaign_analytics["contacted_count"]
                    if "reply_count_unique" in campaign_analytics:
                        summary["replied"] = campaign_analytics["reply_count_unique"]
                        if summary["contacted"] > 0:
                            summary["reply_rate"] = round(
                                summary["replied"] / summary["contacted"] * 100, 1
                            )
    except Exception as e:
        summary["error"] = str(e)

    return summary


def _count_leads_this_week(rows: list, header: list[str] = None) -> int:
    """Count leads added in the last 7 days."""
    from datetime import date, datetime, timedelta

    # Find date_added column from header, fall back to COL position
    date_col = COL.get("date_added", 14)
    if header:
        for i, h in enumerate(header):
            if h.strip().lower() == "date added":
                date_col = i
                break

    cutoff = date.today() - timedelta(days=7)
    count = 0
    for row in rows:
        if len(row) > date_col:
            val = (row[date_col] or "").strip()
            if val:
                try:
                    d = datetime.strptime(val.split()[0], "%Y-%m-%d").date()
                    if d >= cutoff:
                        count += 1
                except (ValueError, IndexError):
                    pass
    return count


