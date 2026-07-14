from __future__ import annotations

import base64
from datetime import date

from fastapi.testclient import TestClient

from dashboard.app import main as dashboard_main
from src.crm import pt_logistics_sheet as sheet_mod
from src.crm.pt_logistics_sheet import FIELD_ALIASES, PTLogisticsCRM, PT_LOGISTICS_HEADERS


def _crm_with_rows(rows: list[dict]) -> PTLogisticsCRM:
    crm = PTLogisticsCRM.__new__(PTLogisticsCRM)
    headers = list(PT_LOGISTICS_HEADERS)
    crm._headers = headers
    crm._columns = {field: headers.index(header) for field, header in FIELD_ALIASES.items() if header in headers}
    crm._cache = []
    for row in rows:
        raw = [""] * len(headers)
        for field, value in row.items():
            header = FIELD_ALIASES.get(field)
            if header in headers:
                raw[headers.index(header)] = value
        crm._cache.append(raw)
    crm._activity_rows = lambda: []
    crm._stage_event_rows = lambda: []
    return crm


def test_data_apis_require_basic_auth(monkeypatch):
    monkeypatch.setenv("DASHBOARD_USER", "jose")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "secret")
    dashboard_main.crm = None
    client = TestClient(dashboard_main.app)

    unauthenticated = client.get("/api/stats")
    assert unauthenticated.status_code == 401
    assert unauthenticated.headers["www-authenticate"] == "Basic"

    token = base64.b64encode(b"jose:secret").decode()
    authenticated = client.get("/api/stats", headers={"Authorization": f"Basic {token}"})
    assert authenticated.status_code == 503
    assert authenticated.json()["error"] == "PT Logistics CRM not initialized"


def test_auth_fails_closed_when_dashboard_credentials_are_missing(monkeypatch):
    monkeypatch.delenv("DASHBOARD_USER", raising=False)
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    dashboard_main.crm = None
    client = TestClient(dashboard_main.app)
    token = base64.b64encode(b"admin:changeme").decode()

    response = client.get("/api/stats", headers={"Authorization": f"Basic {token}"})

    assert response.status_code == 503
    assert response.json()["detail"] == "Dashboard authentication is not configured"


def test_account_profile_after_meeting_booked_collects_context_and_timeline():
    crm = _crm_with_rows([
        {
            "id": "lead-1",
            "company": "Acme Logistics",
            "contact": "Ana",
            "phone": "+351111",
            "email": "ana@acme.test",
            "stage": "Meeting Booked",
            "notes": "[2026-07-14 10:00] Granola: wants cross-border quotes\n---\nInitial call notes",
            "last_touch_type": "Meeting Booked",
            "proposal_sent": "2026/07/10",
            "proposal_value": "12000",
            "proposal_probability": "60",
            "forecast_category": "Commit",
            "meeting_date": "2026/07/15",
            "dashboard_touched": "2026/07/14",
        }
    ])
    crm._activity_rows = lambda: [
        {
            "Timestamp": "2026-07-14 10:00:00",
            "Date": "2026-07-14",
            "Event Type": "meeting",
            "Lead Key": "lead-1",
            "Company": "Acme Logistics",
            "Notes": "Granola transcript attached",
        },
        {
            "Timestamp": "2026-07-13 09:00:00",
            "Date": "2026-07-13",
            "Event Type": "email",
            "Lead Key": "lead-1",
            "Company": "Acme Logistics",
            "Email Task": "Proposal sent",
            "Notes": "Sent proposal",
        },
    ]

    profiles = crm.get_account_profiles(date(2026, 7, 14), stage="Meeting Booked")

    assert len(profiles) == 1
    profile = profiles[0]
    assert profile["company"] == "Acme Logistics"
    assert profile["meeting"]["date_iso"] == "2026-07-15"
    assert profile["proposal"]["weighted_value"] == 7200
    assert "Granola" in profile["notes"][0]
    assert [event["event_type"] for event in profile["timeline"]] == ["meeting", "email"]


def test_portfolio_summary_tracks_values_followups_duration_outcomes_and_forecast():
    crm = _crm_with_rows([
        {
            "id": "open",
            "company": "OpenCo",
            "stage": "Proposal Sent",
            "proposal_sent": "2026/07/01",
            "proposal_status": "Sent",
            "proposal_value": "10000",
            "proposal_probability": "50",
            "forecast_category": "Best Case",
            "proposal_next_action_due": "2026/07/13",
        },
        {
            "id": "won",
            "company": "WonCo",
            "stage": "Meeting Booked",
            "proposal_sent": "2026/07/02",
            "proposal_status": "Won",
            "proposal_value": "20000",
            "proposal_probability": "100",
            "forecast_category": "Commit",
            "proposal_outcome": "Won",
        },
        {
            "id": "lost",
            "company": "LostCo",
            "stage": "Lost",
            "proposal_sent": "2026/07/03",
            "proposal_status": "Lost",
            "proposal_value": "30000",
            "proposal_probability": "0",
            "forecast_category": "Closed Lost",
            "proposal_outcome": "Lost",
        },
    ])

    summary = crm.get_portfolio_summary(date(2026, 7, 14))

    assert summary["counts"] == {"open": 1, "won": 1, "lost": 1, "all": 3}
    assert summary["value"]["open"] == 10000
    assert summary["value"]["won"] == 20000
    assert summary["value"]["lost"] == 30000
    assert summary["weighted_forecast"] == 25000
    assert summary["followups_due"] == 1
    assert summary["average_open_duration_days"] == 13
    assert summary["forecast_by_category"]["Best Case"] == 5000
    assert summary["forecast_by_category"]["Commit"] == 20000


def test_recommendations_prioritize_overdue_followups_and_stale_commit_proposals():
    crm = _crm_with_rows([
        {
            "id": "stale",
            "company": "StaleCo",
            "stage": "Proposal Sent",
            "proposal_sent": "2026/07/01",
            "proposal_status": "Sent",
            "proposal_value": "18000",
            "proposal_probability": "80",
            "forecast_category": "Commit",
            "proposal_next_action_due": "2026/07/10",
        },
        {
            "id": "meeting",
            "company": "MeetingCo",
            "stage": "Meeting Booked",
            "meeting_date": "2026/07/14",
            "notes": "Needs proposal after call",
        },
    ])

    recs = crm.get_recommendations(date(2026, 7, 14))

    assert recs[0]["lead_id"] == "stale"
    assert recs[0]["priority"] == "high"
    assert "overdue" in recs[0]["reason"].lower()
    assert any(rec["lead_id"] == "meeting" and rec["action"] == "Prepare meeting follow-up" for rec in recs)
