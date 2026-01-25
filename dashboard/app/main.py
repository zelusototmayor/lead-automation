"""Lead Automation Dashboard - FastAPI Application"""

import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# Add parent directory to path so we can import from src
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.crm.sheets import GoogleSheetsCRM

# Import directly to avoid loading personalize.py which requires anthropic
from src.outreach.instantly_client import InstantlyClient

from .auth import authenticate
from .metrics import calculate_metrics

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
SHEET_NAME = os.getenv("SHEET_NAME", "Leads")

# Global CRM instance
crm: GoogleSheetsCRM | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize CRM on startup."""
    global crm
    try:
        crm = GoogleSheetsCRM(
            credentials_file=CREDENTIALS_FILE,
            spreadsheet_id=SPREADSHEET_ID,
            sheet_name=SHEET_NAME,
        )
        print(f"CRM initialized: {SPREADSHEET_ID}")
    except Exception as e:
        print(f"Failed to initialize CRM: {e}")
        crm = None
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


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Main dashboard view - publicly accessible."""
    metrics = {}
    error = None

    if crm:
        try:
            # Get all data from Google Sheets
            all_rows = crm.sheet.get_all_values()[1:]  # Skip header
            metrics = calculate_metrics(all_rows)
        except Exception as e:
            error = f"Failed to load data: {e}"
    else:
        error = "CRM not initialized"

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "username": None,  # Not logged in by default
            "metrics": metrics,
            "error": error,
        },
    )


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


@app.get("/api/metrics")
async def api_metrics(username: str = Depends(authenticate)):
    """API endpoint for metrics (for AJAX refresh)."""
    if not crm:
        return JSONResponse({"error": "CRM not initialized"}, status_code=503)

    try:
        all_rows = crm.sheet.get_all_values()[1:]
        metrics = calculate_metrics(all_rows)
        return JSONResponse(metrics)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/sync-replies")
async def sync_replies(username: str = Depends(authenticate)):
    """Sync replies from Instantly to CRM."""
    if not crm:
        return JSONResponse({"error": "CRM not initialized"}, status_code=503)

    if not INSTANTLY_API_KEY:
        return JSONResponse(
            {"error": "INSTANTLY_API_KEY not configured"},
            status_code=503
        )

    try:
        results = _sync_instantly_replies(INSTANTLY_API_KEY, crm)
        return JSONResponse(results)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


def _sync_instantly_replies(api_key: str, crm_instance: GoogleSheetsCRM) -> dict:
    """Sync replies from Instantly to CRM (inline implementation)."""
    instantly = InstantlyClient(api_key)
    results = {
        "campaigns_checked": 0,
        "replies_found": 0,
        "crm_updated": 0,
        "already_synced": 0,
        "not_in_crm": 0,
        "errors": []
    }

    # Get all campaigns
    campaigns = instantly.list_campaigns()
    results["campaigns_checked"] = len(campaigns)

    for campaign in campaigns:
        campaign_id = campaign.get("id")
        if not campaign_id:
            continue

        try:
            # Get leads from campaign
            offset = 0
            limit = 100

            while True:
                result = instantly._make_request(
                    "GET",
                    "lead/list",
                    params={
                        "campaign_id": campaign_id,
                        "limit": limit,
                        "skip": offset
                    }
                )

                if not result:
                    break

                leads = result.get("leads", []) if isinstance(result, dict) else result
                if not leads:
                    break

                for lead in leads:
                    # Check if lead has replied
                    status = lead.get("status", "").lower()
                    has_replied = (
                        status == "replied" or
                        lead.get("replied", False) or
                        lead.get("reply_count", 0) > 0
                    )

                    if not has_replied:
                        continue

                    results["replies_found"] += 1
                    email = lead.get("email")
                    if not email:
                        continue

                    # Find lead in CRM
                    crm_lead = crm_instance.find_lead_by_email(email)
                    if not crm_lead:
                        results["not_in_crm"] += 1
                        continue

                    # Check if already synced
                    if crm_lead.get("response"):
                        results["already_synced"] += 1
                        continue

                    # Update CRM
                    lead_id = crm_lead.get("id")
                    reply_info = lead.get("reply_text") or f"Replied (synced from Instantly)"

                    if crm_instance.mark_response_received(lead_id, reply_info):
                        results["crm_updated"] += 1
                    else:
                        results["errors"].append(f"Failed to update {email}")

                if len(leads) < limit:
                    break
                offset += limit

        except Exception as e:
            results["errors"].append(f"Error in campaign {campaign_id}: {str(e)}")

    return results
