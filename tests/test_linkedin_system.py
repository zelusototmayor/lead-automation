"""
LinkedIn Outbound System — Integration Tests
==============================================
Tests all module logic with mock data. No external API calls needed.

Run:  python tests/test_linkedin_system.py
"""

import sys
from pathlib import Path
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).parent.parent))

# ============================================================
# RESULTS TRACKER
# ============================================================
_results = {"passed": 0, "failed": 0, "errors": []}


def test(name):
    """Decorator that runs a test and tracks pass/fail."""
    def decorator(func):
        def wrapper():
            try:
                func()
                _results["passed"] += 1
                print(f"  ✓ {name}")
            except AssertionError as e:
                _results["failed"] += 1
                _results["errors"].append((name, str(e)))
                print(f"  ✗ {name}: {e}")
            except Exception as e:
                _results["failed"] += 1
                _results["errors"].append((name, f"ERROR: {e}"))
                print(f"  ✗ {name}: ERROR — {e}")
        wrapper._test = True
        wrapper._name = name
        return wrapper
    return decorator


# This is a legacy decorator, not a pytest test function.
setattr(test, "__test__", False)


# ============================================================
# 1. CONFIG MODULE TESTS
# ============================================================

print("\n" + "=" * 60)
print("1. CONFIG MODULE")
print("=" * 60)

from src.linkedin.config import (  # noqa: E402
    LINKEDIN_CRM_HEADERS,
    LCOL,
    SELECTION_CRITERIA,
    SAFETY_LIMITS,
    DM_TEMPLATES,
    DM1_VARIANTS,
    DM1_VARIANT_KEYS,

    get_tier,
    get_next_dm1_variant,
    get_dm1_template,
)


@test("CRM headers include DM 1 Variant column")
def _():
    assert "DM 1 Variant" in LINKEDIN_CRM_HEADERS
    idx = LINKEDIN_CRM_HEADERS.index("DM 1 Variant")
    # Should be between Connection Accepted and DM 1 Sent
    assert LINKEDIN_CRM_HEADERS[idx - 1] == "Connection Accepted"
    assert LINKEDIN_CRM_HEADERS[idx + 1] == "DM 1 Sent"
_()


@test("LCOL maps all headers to correct indices")
def _():
    assert len(LCOL) == len(LINKEDIN_CRM_HEADERS), \
        f"LCOL has {len(LCOL)} keys but headers has {len(LINKEDIN_CRM_HEADERS)} columns"
    # Spot check key columns
    assert LCOL["id"] == 0
    assert LCOL["company"] == 1
    assert LCOL["status"] == LINKEDIN_CRM_HEADERS.index("Status")
    assert "dm_1_variant" in LCOL
_()


@test("DM1_VARIANTS is [A, B, C]")
def _():
    assert DM1_VARIANTS == ["A", "B", "C"]
    assert DM1_VARIANT_KEYS == ["dm1_A", "dm1_B", "dm1_C"]
_()


@test("All 3 DM1 variant templates exist in DM_TEMPLATES")
def _():
    for key in DM1_VARIANT_KEYS:
        assert key in DM_TEMPLATES, f"Missing template: {key}"
        t = DM_TEMPLATES[key]
        assert "system_prompt" in t, f"{key} missing system_prompt"
        assert "user_prompt" in t, f"{key} missing user_prompt"
        assert "variant" in t, f"{key} missing variant field"
        assert "angle" in t, f"{key} missing angle field"
_()


@test("DM2 and DM3 templates exist")
def _():
    for key in ["dm2", "dm3"]:
        assert key in DM_TEMPLATES, f"Missing template: {key}"
        t = DM_TEMPLATES[key]
        assert "system_prompt" in t
        assert "user_prompt" in t
_()


@test("DM1 templates contain all expected placeholders")
def _():
    placeholders = ["{first_name}", "{company}", "{job_hiring}"]
    for key in DM1_VARIANT_KEYS:
        prompt = DM_TEMPLATES[key]["user_prompt"]
        for p in placeholders:
            assert p in prompt, f"{key} user_prompt missing placeholder {p}"
_()


@test("get_tier() returns correct tiers")
def _():
    assert get_tier(0) == "small"
    assert get_tier(5) == "small"
    assert get_tier(19) == "small"
    assert get_tier(20) == "medium"
    assert get_tier(50) == "medium"
    assert get_tier(51) == "large"
    assert get_tier(5000) == "large"
    assert get_tier(None) == "small"
_()


@test("get_next_dm1_variant() round-robins correctly")
def _():
    # All zero → should pick A (first in list)
    assert get_next_dm1_variant({"A": 0, "B": 0, "C": 0}) == "A"
    # A has 1 → pick B
    assert get_next_dm1_variant({"A": 1, "B": 0, "C": 0}) == "B"
    # A=1, B=1 → pick C
    assert get_next_dm1_variant({"A": 1, "B": 1, "C": 0}) == "C"
    # All equal → pick A
    assert get_next_dm1_variant({"A": 5, "B": 5, "C": 5}) == "A"
    # Empty dict → pick A
    assert get_next_dm1_variant({}) == "A"
    # Unbalanced → pick the lowest
    assert get_next_dm1_variant({"A": 10, "B": 8, "C": 9}) == "B"
_()


@test("get_next_dm1_variant() distributes evenly over 12 prospects")
def _():
    counts = {"A": 0, "B": 0, "C": 0}
    for _ in range(12):
        v = get_next_dm1_variant(counts)
        counts[v] += 1
    assert counts == {"A": 4, "B": 4, "C": 4}, f"Uneven: {counts}"
_()


@test("get_dm1_template() returns correct variant")
def _():
    for v in ["A", "B", "C"]:
        t = get_dm1_template(v)
        assert t["variant"] == v, f"Expected variant {v}, got {t.get('variant')}"
    # Unknown variant falls back to A
    t = get_dm1_template("Z")
    assert t["variant"] == "A"
_()


@test("SELECTION_CRITERIA has all 3 tiers with required fields")
def _():
    for tier in ["small", "medium", "large"]:
        assert tier in SELECTION_CRITERIA
        c = SELECTION_CRITERIA[tier]
        assert "max_employees" in c
        assert "title_searches" in c
        assert isinstance(c["title_searches"], list)
        assert len(c["title_searches"]) >= 2
        assert "seniority_keywords" in c
_()


@test("SAFETY_LIMITS has all required keys")
def _():
    required = [
        "max_connections_per_day", "max_connections_per_week",
        "max_dms_per_day", "delay_between_actions_min",
        "dm2_delay_days", "dm3_delay_days",
        "business_hours_start", "business_hours_end",
    ]
    for key in required:
        assert key in SAFETY_LIMITS, f"Missing: {key}"
    assert SAFETY_LIMITS["max_connections_per_day"] == 20
    assert SAFETY_LIMITS["max_connections_per_week"] == 100
_()


# ============================================================
# 2. PROSPECT FINDER TESTS
# ============================================================

print("\n" + "=" * 60)
print("2. PROSPECT FINDER")
print("=" * 60)

from src.linkedin.prospect_finder import (  # noqa: E402
    should_skip_prospect,
    get_search_titles,
    get_browser_instructions as pf_browser_instructions,
)


@test("should_skip_prospect() skips recruiters and HR")
def _():
    assert should_skip_prospect("Technical Recruiter") is True
    assert should_skip_prospect("Talent Acquisition Manager") is True
    assert should_skip_prospect("HR Director") is True
    assert should_skip_prospect("People Operations Lead") is True
    assert should_skip_prospect("Intern - Sales") is True
_()


@test("should_skip_prospect() skips former employees")
def _():
    assert should_skip_prospect("", "formerly at Acme Corp") is True
    assert should_skip_prospect("ex-CRO at TechCo") is True
    assert should_skip_prospect("former VP Sales") is True
_()


@test("should_skip_prospect() allows valid titles")
def _():
    assert should_skip_prospect("VP of Sales") is False
    assert should_skip_prospect("CEO") is False
    assert should_skip_prospect("Founder") is False
    assert should_skip_prospect("Head of Revenue") is False
    assert should_skip_prospect("Director of Business Development") is False
_()


@test("get_search_titles() returns size-appropriate titles")
def _():
    small = get_search_titles(10)
    assert any("Founder" in title for group in small for title in group)

    medium = get_search_titles(30)
    assert any("VP" in title for group in medium for title in group)

    large = get_search_titles(100)
    assert any("CRO" in title or "Chief Revenue" in title for group in large for title in group)
_()


@test("prospect_finder browser instructions include key fields")
def _():
    company = {
        "company": "TestCorp",
        "company_linkedin": "https://linkedin.com/company/testcorp",
        "employee_count": 35,
        "country": "US",
    }
    instr = pf_browser_instructions(company)
    assert "TestCorp" in instr
    assert "linkedin.com/company/testcorp" in instr
    assert "medium" in instr  # tier
_()


# ============================================================
# 3. CONNECTION REQUESTER TESTS
# ============================================================

print("\n" + "=" * 60)
print("3. CONNECTION REQUESTER")
print("=" * 60)

from src.linkedin.connection_requester import (  # noqa: E402
    check_safety_limits,
    get_browser_instructions as cr_browser_instructions,
)


@test("check_safety_limits() structure is correct")
def _():
    # Mock CRM with zero week count
    mock_crm = MagicMock()
    mock_crm.get_week_connection_count.return_value = 0

    checks = check_safety_limits(mock_crm)

    assert "is_weekday" in checks
    assert "is_business_hours" in checks
    assert "week_count" in checks
    assert "week_limit" in checks
    assert "daily_limit" in checks
    assert "can_send" in checks
    assert "remaining_today" in checks
    assert isinstance(checks["remaining_today"], int)
_()


@test("check_safety_limits() respects weekly cap")
def _():
    mock_crm = MagicMock()
    mock_crm.get_week_connection_count.return_value = 100  # at limit

    checks = check_safety_limits(mock_crm)
    assert checks["week_ok"] is False
    assert checks["remaining_today"] == 0
_()


@test("check_safety_limits() calculates remaining correctly")
def _():
    mock_crm = MagicMock()
    mock_crm.get_week_connection_count.return_value = 85

    checks = check_safety_limits(mock_crm)
    assert checks["remaining_today"] == min(20, 100 - 85)  # 15
_()


@test("connection requester browser instructions say NO note")
def _():
    prospects = [{
        "contact_name": "Jane Smith",
        "title": "VP Sales",
        "company": "AcmeCo",
        "linkedin_url": "https://linkedin.com/in/janesmith",
        "id": "LKDN-001",
    }]
    instr = cr_browser_instructions(prospects)
    assert "NOT include a connection note" in instr or "NO note" in instr
    assert "Jane Smith" in instr
    assert "LKDN-001" in instr
_()


# ============================================================
# 4. SHEETS CRM TESTS (with mocked Google Sheets)
# ============================================================

print("\n" + "=" * 60)
print("4. SHEETS CRM (mocked)")
print("=" * 60)


def make_mock_crm(rows=None):
    """Create a LinkedInSheetsCRM with mocked Google Sheets backend."""
    with patch("src.linkedin.sheets_crm.Credentials") as MockCreds, \
         patch("src.linkedin.sheets_crm.gspread") as MockGspread:

        mock_creds = MagicMock()
        MockCreds.from_service_account_file.return_value = mock_creds

        mock_sheet = MagicMock()
        mock_sheet.row_values.return_value = LINKEDIN_CRM_HEADERS

        # Build mock data
        if rows is None:
            rows = []
        mock_sheet.get_all_values.return_value = [LINKEDIN_CRM_HEADERS] + rows

        mock_spreadsheet = MagicMock()
        mock_spreadsheet.worksheet.return_value = mock_sheet

        mock_client = MagicMock()
        mock_client.open_by_key.return_value = mock_spreadsheet
        MockGspread.authorize.return_value = mock_client

        crm = __import__("src.linkedin.sheets_crm", fromlist=["LinkedInSheetsCRM"]).LinkedInSheetsCRM(
            credentials_file="fake.json",
            spreadsheet_id="fake-id",
        )
        crm._sheet = mock_sheet
        return crm


def make_row(**kwargs):
    """Build a CRM row from keyword args."""
    row = [""] * len(LINKEDIN_CRM_HEADERS)
    for key, val in kwargs.items():
        idx = LCOL.get(key)
        if idx is not None:
            row[idx] = str(val)
    return row


@test("CRM get_variant_counts() counts A/B/C correctly")
def _():
    rows = [
        make_row(id="L1", company="Co1", status="DM 1", dm_1_variant="A", dm_1_sent="2026-03-25 10:00"),
        make_row(id="L2", company="Co2", status="DM 1", dm_1_variant="A", dm_1_sent="2026-03-25 10:05"),
        make_row(id="L3", company="Co3", status="DM 1", dm_1_variant="B", dm_1_sent="2026-03-25 10:10"),
        make_row(id="L4", company="Co4", status="DM 1", dm_1_variant="C", dm_1_sent="2026-03-25 10:15"),
        make_row(id="L5", company="Co5", status="Connected"),  # no variant yet
    ]
    crm = make_mock_crm(rows)
    counts = crm.get_variant_counts()
    assert counts == {"A": 2, "B": 1, "C": 1}, f"Got: {counts}"
_()


@test("CRM get_variant_reply_rates() calculates rates correctly")
def _():
    rows = [
        make_row(id="L1", status="DM 1", dm_1_variant="A", dm_1_sent="2026-03-20 10:00"),
        make_row(id="L2", status="Replied", dm_1_variant="A", dm_1_sent="2026-03-20 10:05", reply="Interested!"),
        make_row(id="L3", status="DM 1", dm_1_variant="B", dm_1_sent="2026-03-20 10:10"),
        make_row(id="L4", status="Replied", dm_1_variant="B", dm_1_sent="2026-03-20 10:15", reply="Tell me more"),
        make_row(id="L5", status="Replied", dm_1_variant="B", dm_1_sent="2026-03-20 10:20", reply="Sure"),
        make_row(id="L6", status="DM 1", dm_1_variant="C", dm_1_sent="2026-03-20 10:25"),
    ]
    crm = make_mock_crm(rows)
    rates = crm.get_variant_reply_rates()

    # A: 2 sent, 1 replied → 0.5
    assert rates["A"]["sent"] == 2
    assert rates["A"]["replied"] == 1
    assert rates["A"]["rate"] == 0.5

    # B: 3 sent, 2 replied → 0.67
    assert rates["B"]["sent"] == 3
    assert rates["B"]["replied"] == 2
    assert rates["B"]["rate"] == 0.67

    # C: 1 sent, 0 replied → 0.0
    assert rates["C"]["sent"] == 1
    assert rates["C"]["replied"] == 0
    assert rates["C"]["rate"] == 0.0
_()


@test("CRM get_variant_reply_rates() handles zero sends")
def _():
    crm = make_mock_crm([])
    rates = crm.get_variant_reply_rates()
    for v in DM1_VARIANTS:
        assert rates[v]["sent"] == 0
        assert rates[v]["rate"] == 0.0
_()


@test("CRM mark_dm_sent() stores variant for DM 1")
def _():
    rows = [
        make_row(id="L1", company="TestCo", status="Connected"),
    ]
    crm = make_mock_crm(rows)

    result = crm.mark_dm_sent("L1", dm_number=1, variant="B")
    assert result is True

    # Check the cache was updated
    updated_row = crm._cache[0]
    variant_idx = LCOL["dm_1_variant"]
    assert updated_row[variant_idx] == "B", f"Variant not stored: {updated_row[variant_idx]}"
_()


@test("CRM mark_dm_sent() does not store variant for DM 2/3")
def _():
    rows = [
        make_row(id="L1", company="TestCo", status="DM 1", dm_1_sent="2026-03-20 10:00"),
    ]
    crm = make_mock_crm(rows)

    result = crm.mark_dm_sent("L1", dm_number=2)
    assert result is True
    # Variant column should be untouched (still empty)
    variant_idx = LCOL["dm_1_variant"]
    assert crm._cache[0][variant_idx] == ""
_()


@test("CRM get_dm_ready() returns Connected prospects for DM 1")
def _():
    rows = [
        make_row(id="L1", company="Co1", status="Connected"),
        make_row(id="L2", company="Co2", status="Connected"),
        make_row(id="L3", company="Co3", status="DM 1", dm_1_sent="2026-03-20 10:00"),
        make_row(id="L4", company="Co4", status="New"),
    ]
    crm = make_mock_crm(rows)
    ready = crm.get_dm_ready(1)
    assert len(ready) == 2
    assert ready[0]["id"] == "L1"
    assert ready[1]["id"] == "L2"
_()


@test("CRM get_dm_ready() respects DM 2 delay")
def _():
    now = datetime.now()
    old_date = (now - timedelta(days=6)).strftime("%Y-%m-%d %H:%M")
    recent_date = (now - timedelta(days=2)).strftime("%Y-%m-%d %H:%M")

    rows = [
        make_row(id="L1", status="DM 1", dm_1_sent=old_date),    # 6 days ago → ready
        make_row(id="L2", status="DM 1", dm_1_sent=recent_date), # 2 days ago → not ready
    ]
    crm = make_mock_crm(rows)
    ready = crm.get_dm_ready(2)
    assert len(ready) == 1
    assert ready[0]["id"] == "L1"
_()


@test("CRM get_dm_ready() skips prospects who already replied")
def _():
    now = datetime.now()
    old_date = (now - timedelta(days=6)).strftime("%Y-%m-%d %H:%M")

    rows = [
        make_row(id="L1", status="DM 1", dm_1_sent=old_date, reply="Thanks!"),  # replied
        make_row(id="L2", status="DM 1", dm_1_sent=old_date),                   # no reply
    ]
    crm = make_mock_crm(rows)
    ready = crm.get_dm_ready(2)
    assert len(ready) == 1
    assert ready[0]["id"] == "L2"
_()


@test("CRM get_manual_toggles() detects manual YES toggles")
def _():
    rows = [
        make_row(id="L1", status="Request Sent", **{"connected_manual": "YES"}),
        make_row(id="L2", status="Request Sent", **{"connected_manual": "FALSE"}),
        make_row(id="L3", status="Connected", **{"connected_manual": "YES"}),  # already connected
    ]
    crm = make_mock_crm(rows)
    toggled = crm.get_manual_toggles()
    # L1 should be returned (YES + not yet Connected), L2 no, L3 already connected
    assert len(toggled) == 1
    assert toggled[0]["id"] == "L1"
_()


@test("CRM get_week_connection_count() counts this week only")
def _():
    now = datetime.now()
    this_week = now.strftime("%Y-%m-%d %H:%M")
    last_week = (now - timedelta(days=8)).strftime("%Y-%m-%d %H:%M")

    rows = [
        make_row(id="L1", connection_sent=this_week),
        make_row(id="L2", connection_sent=this_week),
        make_row(id="L3", connection_sent=last_week),  # last week
    ]
    crm = make_mock_crm(rows)
    count = crm.get_week_connection_count()
    assert count == 2, f"Expected 2, got {count}"
_()


@test("CRM get_stats() includes variant counts and reply rates")
def _():
    rows = [
        make_row(id="L1", status="DM 1", dm_1_variant="A", dm_1_sent="2026-03-25 10:00"),
        make_row(id="L2", status="Replied", dm_1_variant="B", dm_1_sent="2026-03-25 10:00", reply="Yes"),
    ]
    crm = make_mock_crm(rows)
    stats = crm.get_stats()
    assert "variant_counts" in stats
    assert "variant_reply_rates" in stats
    assert stats["variant_counts"]["A"] == 1
    assert stats["variant_counts"]["B"] == 1
_()


@test("CRM deduplicates by LinkedIn URL")
def _():
    rows = [
        make_row(id="L1", linkedin_url="https://linkedin.com/in/janesmith"),
    ]
    crm = make_mock_crm(rows)
    urls = crm.get_all_linkedin_urls()
    assert "https://linkedin.com/in/janesmith" in urls
_()


# ============================================================
# 5. DM SENDER TESTS
# ============================================================

print("\n" + "=" * 60)
print("5. DM SENDER")
print("=" * 60)

from src.linkedin.dm_sender import (  # noqa: E402
    personalize_dm,
    get_browser_instructions as dm_browser_instructions,
)


@test("personalize_dm() formats user prompt with prospect data")
def _():
    """Test that the template formatting works (mock the API call)."""
    prospect = {
        "contact_name": "John Doe",
        "title": "VP of Sales",
        "company": "WidgetCorp",
        "industry": "SaaS",
        "job_hiring": "SDR",
        "country": "US",
        "description_snippet": "B2B SaaS for widgets",
    }

    # Mock the API call to return a fixed response
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "content": [{"text": "John, saw WidgetCorp is hiring SDRs — we automate that. 15 min?"}]
    }
    mock_response.raise_for_status = MagicMock()

    with patch("src.linkedin.dm_sender.requests.post", return_value=mock_response) as mock_post:
        result = personalize_dm("fake-key", 1, prospect, variant="A")

        # Check API was called
        assert mock_post.called
        call_kwargs = mock_post.call_args
        body = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")

        # Check system prompt came from variant A
        assert "direct" in body["system"].lower() or "Direct" in body["system"]

        # Check user prompt was filled in
        user_msg = body["messages"][0]["content"]
        assert "John" in user_msg
        assert "WidgetCorp" in user_msg
        assert "SDR" in user_msg

    assert "John" in result
    assert "WidgetCorp" in result
_()


@test("personalize_dm() uses variant-specific template for DM 1")
def _():
    prospect = {
        "contact_name": "Jane Doe",
        "title": "CEO",
        "company": "StartupX",
        "industry": "FinTech",
        "job_hiring": "BDR",
        "country": "UK",
        "description_snippet": "FinTech startup",
    }

    mock_response = MagicMock()
    mock_response.json.return_value = {"content": [{"text": "test message"}]}
    mock_response.raise_for_status = MagicMock()

    # Test each variant
    for variant, expected_angle in [("A", "direct"), ("B", "result"), ("C", "challenge")]:
        with patch("src.linkedin.dm_sender.requests.post", return_value=mock_response) as mock_post:
            personalize_dm("fake-key", 1, prospect, variant=variant)
            body = mock_post.call_args.kwargs.get("json") or mock_post.call_args[1].get("json")
            system = body["system"].lower()
            assert expected_angle in system, \
                f"Variant {variant} should use {expected_angle} angle, got: {system[:80]}"
_()


@test("personalize_dm() uses standard template for DM 2/3")
def _():
    prospect = {"contact_name": "Bob", "company": "Acme", "job_hiring": "SDR"}

    mock_response = MagicMock()
    mock_response.json.return_value = {"content": [{"text": "follow up"}]}
    mock_response.raise_for_status = MagicMock()

    with patch("src.linkedin.dm_sender.requests.post", return_value=mock_response) as mock_post:
        personalize_dm("fake-key", 2, prospect, variant="")
        body = mock_post.call_args.kwargs.get("json") or mock_post.call_args[1].get("json")
        system = body["system"].lower()
        assert "follow-up" in system or "follow up" in system
_()


@test("DM browser instructions include variant info")
def _():
    messages = [{
        "contact_name": "Jane Smith",
        "company": "TechCo",
        "linkedin_url": "https://linkedin.com/in/janesmith",
        "id": "LKDN-001",
        "dm_number": 1,
        "dm_variant": "B",
        "dm_text": "Hey Jane, noticed TechCo is hiring SDRs...",
    }]
    instr = dm_browser_instructions(messages)
    assert "variant B" in instr
    assert "LKDN-001" in instr
    assert "Hey Jane" in instr
_()


@test("DM browser instructions omit variant for DM 2/3")
def _():
    messages = [{
        "contact_name": "Bob Lee",
        "company": "SalesCo",
        "linkedin_url": "https://linkedin.com/in/boblee",
        "id": "LKDN-002",
        "dm_number": 2,
        "dm_variant": "",
        "dm_text": "Quick follow up...",
    }]
    instr = dm_browser_instructions(messages)
    assert "variant" not in instr.split("DM #2")[1].split("\n")[0]
_()


# ============================================================
# 6. ACCEPTANCE MONITOR TESTS
# ============================================================

print("\n" + "=" * 60)
print("6. ACCEPTANCE MONITOR")
print("=" * 60)

from src.linkedin.acceptance_monitor import (  # noqa: E402
    check_manual_toggles,
    get_browser_instructions as am_browser_instructions,
)


@test("check_manual_toggles() calls mark_connected on toggled prospects")
def _():
    toggled_prospects = [
        {"id": "L1", "contact_name": "Jane", "company": "Co1"},
        {"id": "L2", "contact_name": "Bob", "company": "Co2"},
    ]
    mock_crm = MagicMock()
    mock_crm.get_manual_toggles.return_value = toggled_prospects
    mock_crm.mark_connected.return_value = True

    result = check_manual_toggles(mock_crm)
    assert len(result) == 2
    assert mock_crm.mark_connected.call_count == 2
    mock_crm.mark_connected.assert_any_call("L1")
    mock_crm.mark_connected.assert_any_call("L2")
_()


@test("acceptance monitor browser instructions list pending prospects")
def _():
    pending = [
        {"contact_name": "Alice", "company": "AliceCo", "id": "LKDN-010"},
        {"contact_name": "Charlie", "company": "CharlieCo", "id": "LKDN-011"},
    ]
    instr = am_browser_instructions(pending)
    assert "Alice" in instr
    assert "LKDN-010" in instr
    assert "Charlie" in instr
    assert "invitation-manager" in instr
_()


# ============================================================
# 7. REPLY MONITOR TESTS
# ============================================================

print("\n" + "=" * 60)
print("7. REPLY MONITOR")
print("=" * 60)

from src.linkedin.reply_monitor import (  # noqa: E402
    send_ntfy_notification,
    handle_reply,
    get_browser_instructions as rm_browser_instructions,
)


@test("send_ntfy_notification() posts to correct URL")
def _():
    with patch("src.linkedin.reply_monitor.requests.post") as mock_post:
        send_ntfy_notification("test-topic", "Reply!", "Someone replied")
        mock_post.assert_called_once()
        url = mock_post.call_args[0][0]
        assert "ntfy.sh/test-topic" in url
_()


@test("send_ntfy_notification() silently skips empty topic")
def _():
    with patch("src.linkedin.reply_monitor.requests.post") as mock_post:
        send_ntfy_notification("", "Title", "Body")
        mock_post.assert_not_called()
_()


@test("handle_reply() updates CRM and sends notification")
def _():
    mock_crm = MagicMock()
    config = {"monitoring": {"ntfy_topic": "test-topic"}}

    with patch("src.linkedin.reply_monitor.send_ntfy_notification") as mock_ntfy:
        handle_reply(config, mock_crm, "L1", "Sounds interesting!", "Jane Doe", "TechCo")

        mock_crm.mark_replied.assert_called_once_with("L1", "Sounds interesting!")
        mock_ntfy.assert_called_once()
        ntfy_args = mock_ntfy.call_args
        assert "Jane Doe" in ntfy_args[1].get("title", "") or "Jane Doe" in str(ntfy_args)
_()


@test("reply monitor browser instructions are read-only")
def _():
    active = [
        {"contact_name": "Dave", "company": "DevCo", "id": "LKDN-020", "status": "DM 1"},
    ]
    instr = rm_browser_instructions(active)
    assert "read-only" in instr.lower() or "Do NOT send" in instr
    assert "Dave" in instr
    assert "messaging" in instr.lower()
_()


# ============================================================
# 8. END-TO-END VARIANT ROTATION SIMULATION
# ============================================================

print("\n" + "=" * 60)
print("8. END-TO-END VARIANT ROTATION SIMULATION")
print("=" * 60)


@test("Simulate 30 DM 1 sends with round-robin rotation")
def _():
    """Simulate what happens in a real run() — variant selection + CRM update."""
    counts = {"A": 0, "B": 0, "C": 0}
    assignments = []

    for i in range(30):
        v = get_next_dm1_variant(counts)
        counts[v] += 1
        assignments.append(v)

    # Should be perfectly balanced: 10 each
    assert counts == {"A": 10, "B": 10, "C": 10}, f"Unbalanced: {counts}"
    # Pattern should be A,B,C,A,B,C,...
    assert assignments[:6] == ["A", "B", "C", "A", "B", "C"]
_()


@test("Simulate variant rotation with pre-existing uneven counts")
def _():
    """Start from uneven state (e.g., after a partial previous run)."""
    counts = {"A": 7, "B": 5, "C": 6}

    # Next should be B (lowest)
    v = get_next_dm1_variant(counts)
    assert v == "B"
    counts["B"] += 1  # Now A:7, B:6, C:6

    # Next should be B or C (both at 6, B comes first in list)
    v = get_next_dm1_variant(counts)
    assert v == "B"
    counts["B"] += 1  # Now A:7, B:7, C:6

    # Next should be C (lowest at 6)
    v = get_next_dm1_variant(counts)
    assert v == "C"
_()


@test("Simulate variant rollback on personalization failure")
def _():
    """If Claude API fails, the variant count should be rolled back."""
    counts = {"A": 3, "B": 3, "C": 3}

    # Pick variant
    v = get_next_dm1_variant(counts)
    assert v == "A"
    counts[v] += 1  # A:4, B:3, C:3

    # Simulate failure — roll back
    counts[v] -= 1  # Back to A:3, B:3, C:3

    # Next pick should still be A (back to equal)
    v = get_next_dm1_variant(counts)
    assert v == "A"
_()


# ============================================================
# FINAL REPORT
# ============================================================

print("\n" + "=" * 60)
total = _results["passed"] + _results["failed"]
print(f"RESULTS: {_results['passed']}/{total} passed, {_results['failed']} failed")
print("=" * 60)

if _results["errors"]:
    print("\nFAILURES:")
    for name, err in _results["errors"]:
        print(f"  ✗ {name}")
        print(f"    {err}")

if __name__ == "__main__":
    sys.exit(0 if _results["failed"] == 0 else 1)


def test_linkedin_outbound_checks():
    """Expose the legacy in-module checks as one pytest result."""

    assert _results["failed"] == 0, _results["errors"]
