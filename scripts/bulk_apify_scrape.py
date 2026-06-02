"""
Bulk Apify Scrape — Final $20 Credit Burn
==========================================
One-time massive LinkedIn job scrape before canceling Apify subscription.
Scrapes SDR/BDR hiring signals globally, applies B2B keyword filter,
deduplicates, and pushes to Google Sheets CRM.

NO paid APIs used (no Apollo, no SerpAPI). Just Apify + local filtering.

Budget: ~$20 = ~20,000 job results ($1 per 1,000 jobs)
Actor: HarvestAPI LinkedIn Job Search (harvestapi~linkedin-job-search)

Usage:
    python scripts/bulk_apify_scrape.py                  # Full scrape
    python scripts/bulk_apify_scrape.py --dry-run        # Test without API calls
    python scripts/bulk_apify_scrape.py --budget 10      # Override budget ($)
    python scripts/bulk_apify_scrape.py --csv-only       # Save to CSV, skip Sheets
"""

import os
import sys
import csv
import json
import time
import argparse
import requests
from pathlib import Path
from datetime import datetime
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

LINKEDIN_JOBS_ACTOR = "harvestapi~linkedin-job-search"
APIFY_BASE_URL = "https://api.apify.com/v2"
COST_PER_1000 = 1.0  # $1 per 1,000 jobs

# Job title queries — cast a wide net
JOB_QUERIES = [
    "SDR",
    "BDR",
    "Sales Development Representative",
    "Business Development Representative",
    "Outbound Sales Representative",
    "Inside Sales Representative",
]

# Global locations with budget allocation (max_items per location)
# Bigger markets get more budget. Total ~20,000 items = ~$20.
LOCATION_BATCHES = [
    {"location": "United States",    "max_items": 4000},
    {"location": "United Kingdom",   "max_items": 3000},
    {"location": "Germany",          "max_items": 2000},
    {"location": "Canada",           "max_items": 2000},
    {"location": "Spain",            "max_items": 1500},
    {"location": "France",           "max_items": 1500},
    {"location": "Australia",        "max_items": 1500},
    {"location": "Netherlands",      "max_items": 1500},
    {"location": "Portugal",         "max_items": 1000},
    {"location": "Ireland",          "max_items": 1000},
    {"location": "Sweden",           "max_items": 500},
    {"location": "Denmark",          "max_items": 500},
]
# Total: 20,000 items → $20

# B2B keyword filters (same as eu_outreach.py — no API needed)
B2B_KEYWORDS = [
    "saas", "b2b", "software", "platform", "api", "cloud",
    "enterprise", "automation", "analytics", "data", "fintech",
    "martech", "proptech", "healthtech", "edtech", "devtools",
    "ai ", "machine learning", "cybersecurity", "security",
    "devops", "infrastructure", "payments", "crm", "erp",
]

B2B_JOB_SIGNALS = [
    "outbound", "pipeline", "prospecting", "cold call", "cold email",
    "demo", "qualified leads", "crm", "salesforce", "hubspot",
    "linkedin sales navigator", "decision maker", "c-level",
    "stakeholder", "revenue", "arr", "mrr", "quota",
    "account executive", "closing", "sdr", "bdr",
]

BLACKLIST_KEYWORDS = [
    "restaurant", "hotel", "hospitality", "retail store",
    "staffing agency", "recruitment agency", "nursing",
    "healthcare", "medical", "school", "university",
    "government", "non-profit", "nonprofit", "charity",
    "casino", "gambling",
]

EXCLUDE_COMPANIES = {
    "google", "amazon", "microsoft", "meta", "salesforce",
    "cisco", "oracle", "ibm", "sap", "accenture", "deloitte",
    "mckinsey", "pwc", "ey", "kpmg", "apple", "tesla",
    "uber", "airbnb", "netflix", "spotify",
}


# ---------------------------------------------------------------------------
# Apify API (simplified for bulk use)
# ---------------------------------------------------------------------------

class BulkApifyClient:
    """Minimal Apify client for the bulk scrape — no budget cap."""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }
        self.total_items_fetched = 0
        self.total_runs = 0
        self.estimated_cost = 0.0

    def check_balance(self) -> dict:
        """Check current Apify account balance and usage."""
        try:
            resp = requests.get(
                f"{APIFY_BASE_URL}/users/me",
                headers=self.headers,
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json().get("data", {})
            plan = data.get("plan", {})
            return {
                "username": data.get("username", ""),
                "plan": plan.get("id", ""),
                "monthly_usage_usd": plan.get("monthlyUsageCreditsUsd", 0),
                "remaining_usd": plan.get("usageCreditsAmount", 0)
                    - plan.get("monthlyUsageCreditsUsd", 0),
            }
        except Exception as e:
            return {"error": str(e)}

    def run_search(
        self,
        location: str,
        max_items: int,
        timeout_secs: int = 1800,  # 30 min for large scrapes
        poll_interval: int = 15,
    ) -> list[dict]:
        """Run a single LinkedIn job search for a location."""
        input_data = {
            "jobTitles": JOB_QUERIES,
            "locations": [location],
            "maxItems": max_items,
            "postedLimit": "month",
            "sortBy": "date",
        }

        print(f"  Starting actor for {location} (max {max_items} items)...")

        try:
            resp = requests.post(
                f"{APIFY_BASE_URL}/acts/{LINKEDIN_JOBS_ACTOR}/runs",
                json=input_data,
                headers=self.headers,
                timeout=30,
            )
            resp.raise_for_status()
            run_data = resp.json().get("data", {})
            run_id = run_data.get("id")
            dataset_id = run_data.get("defaultDatasetId")
            self.total_runs += 1
        except requests.RequestException as e:
            print(f"  ERROR starting actor for {location}: {e}")
            return []

        # Poll for completion
        status_url = f"{APIFY_BASE_URL}/actor-runs/{run_id}"
        elapsed = 0
        while elapsed < timeout_secs:
            time.sleep(poll_interval)
            elapsed += poll_interval
            try:
                resp = requests.get(status_url, headers=self.headers, timeout=15)
                resp.raise_for_status()
                status = resp.json().get("data", {}).get("status")
                if status == "SUCCEEDED":
                    break
                elif status in ("FAILED", "ABORTED", "TIMED-OUT"):
                    print(f"  Actor run FAILED for {location}: {status}")
                    return []
                else:
                    mins = elapsed // 60
                    secs = elapsed % 60
                    print(f"  [{mins}m{secs:02d}s] {location}: {status}...")
            except requests.RequestException:
                pass
        else:
            print(f"  Actor run TIMED OUT for {location} ({timeout_secs}s)")
            return []

        # Fetch dataset items (paginated — up to 10,000 per page)
        all_items = []
        offset = 0
        page_size = 2000
        while True:
            try:
                resp = requests.get(
                    f"{APIFY_BASE_URL}/datasets/{dataset_id}/items",
                    headers=self.headers,
                    params={"format": "json", "limit": page_size, "offset": offset},
                    timeout=60,
                )
                resp.raise_for_status()
                items = resp.json()
                if not items:
                    break
                all_items.extend(items)
                offset += len(items)
                if len(items) < page_size:
                    break
            except requests.RequestException as e:
                print(f"  ERROR fetching dataset for {location}: {e}")
                break

        count = len(all_items)
        cost = count * COST_PER_1000 / 1000
        self.total_items_fetched += count
        self.estimated_cost += cost
        print(f"  {location}: {count} jobs fetched (~${cost:.2f})")

        return all_items


# ---------------------------------------------------------------------------
# Data extraction & filtering
# ---------------------------------------------------------------------------

def extract_company(job: dict) -> str:
    for field in ("companyName", "company_name"):
        val = job.get(field)
        if val and isinstance(val, str):
            return val.strip()
    company_obj = job.get("company")
    if isinstance(company_obj, dict) and company_obj.get("name"):
        return company_obj["name"].strip()
    header = job.get("headerCaptionText", "")
    if header and isinstance(header, str):
        first_line = header.split("\n")[0].strip()
        if first_line and not first_line.startswith("http"):
            return first_line
    return ""


def extract_location(job: dict) -> dict:
    loc = job.get("location", {})
    if isinstance(loc, dict):
        parsed = loc.get("parsed", {})
        return {
            "country": parsed.get("country") or parsed.get("countryFull") or "",
            "city": parsed.get("city") or "",
            "country_code": parsed.get("countryCode") or loc.get("countryCode") or "",
            "location_text": loc.get("linkedinText") or parsed.get("text") or "",
        }
    if isinstance(loc, str):
        return {"country": "", "city": "", "country_code": "", "location_text": loc}
    return {"country": "", "city": "", "country_code": "", "location_text": ""}


def is_b2b(description: str, company_name: str) -> bool:
    """B2B filter using keywords — no API calls."""
    text = (description or "").lower() + " " + (company_name or "").lower()

    if any(kw in text for kw in BLACKLIST_KEYWORDS):
        return False

    if any(kw in text for kw in B2B_KEYWORDS):
        return True

    if any(kw in text for kw in B2B_JOB_SIGNALS):
        return True

    return False


def process_jobs(all_jobs: list[dict]) -> list[dict]:
    """Deduplicate, filter B2B, and extract structured data."""
    seen_companies = {}
    skipped_excluded = 0
    skipped_no_company = 0
    skipped_not_b2b = 0
    skipped_duplicate = 0

    for job in all_jobs:
        company = extract_company(job)
        if not company:
            skipped_no_company += 1
            continue

        company_lower = company.lower().strip()

        # Exclude mega-corps
        if any(exc in company_lower for exc in EXCLUDE_COMPANIES):
            skipped_excluded += 1
            continue

        # Dedup by company name
        if company_lower in seen_companies:
            skipped_duplicate += 1
            continue

        # B2B filter
        description = job.get("descriptionText", "") or ""
        if not is_b2b(description, company):
            skipped_not_b2b += 1
            continue

        loc = extract_location(job)

        seen_companies[company_lower] = {
            "company_name": company,
            "job_title": job.get("title", ""),
            "job_url": job.get("linkedinUrl", ""),
            "job_posted_date": job.get("postedDate", ""),
            "employment_type": job.get("employmentType", ""),
            "workplace_type": job.get("workplaceType", ""),
            "country": loc["country"],
            "city": loc["city"],
            "country_code": loc["country_code"],
            "location_text": loc["location_text"],
            "company_linkedin_url": job.get("companyUrl", ""),
            "description_snippet": description[:300],  # Keep first 300 chars for reference
            "scraped_date": datetime.now().strftime("%Y-%m-%d"),
        }

    print(f"\n--- Processing Summary ---")
    print(f"  Total raw jobs:      {len(all_jobs)}")
    print(f"  No company name:     {skipped_no_company}")
    print(f"  Excluded (mega-corp): {skipped_excluded}")
    print(f"  Duplicates:          {skipped_duplicate}")
    print(f"  Not B2B:             {skipped_not_b2b}")
    print(f"  B2B companies kept:  {len(seen_companies)}")

    return list(seen_companies.values())


# ---------------------------------------------------------------------------
# Output: CSV
# ---------------------------------------------------------------------------

CSV_HEADERS = [
    "company_name", "job_title", "job_url", "job_posted_date",
    "employment_type", "workplace_type",
    "country", "city", "country_code", "location_text",
    "company_linkedin_url", "description_snippet", "scraped_date",
]


def save_to_csv(companies: list[dict], output_path: str):
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(companies)
    print(f"\nCSV saved: {output_path} ({len(companies)} rows)")


# ---------------------------------------------------------------------------
# Output: Google Sheets
# ---------------------------------------------------------------------------

def push_to_sheets(companies: list[dict], config_path: str):
    """Push results to a new tab in the existing CRM spreadsheet."""
    import yaml
    import gspread
    from google.oauth2.service_account import Credentials

    # Load config
    env_file = Path(__file__).parent.parent / ".env"
    if env_file.exists():
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    os.environ.setdefault(key.strip(), value.strip())

    with open(config_path) as f:
        config = yaml.safe_load(f)

    spreadsheet_id = config["google_sheets"]["spreadsheet_id"]
    credentials_file = config["google_sheets"]["credentials_file"]

    # Resolve credentials path relative to project root
    project_root = Path(__file__).parent.parent
    creds_path = project_root / credentials_file

    creds = Credentials.from_service_account_file(
        str(creds_path),
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ],
    )
    gc = gspread.authorize(creds)
    spreadsheet = gc.open_by_key(spreadsheet_id)

    # Create or get sheet tab
    sheet_name = f"Apify Bulk {datetime.now().strftime('%Y-%m-%d')}"
    try:
        sheet = spreadsheet.worksheet(sheet_name)
        print(f"  Sheet '{sheet_name}' already exists — appending...")
    except gspread.WorksheetNotFound:
        sheet = spreadsheet.add_worksheet(
            title=sheet_name,
            rows=max(len(companies) + 10, 100),
            cols=len(CSV_HEADERS),
        )
        print(f"  Created new sheet: '{sheet_name}'")

    # Write headers
    headers_display = [
        "Company", "Job Title", "Job URL", "Posted Date",
        "Employment Type", "Workplace Type",
        "Country", "City", "Country Code", "Location",
        "Company LinkedIn", "Description (snippet)", "Scraped Date",
    ]
    sheet.update("A1", [headers_display])

    # Write data in chunks (Google Sheets has a limit of ~50,000 cells per request)
    CHUNK_SIZE = 500
    total_written = 0

    for i in range(0, len(companies), CHUNK_SIZE):
        chunk = companies[i:i + CHUNK_SIZE]
        rows = []
        for c in chunk:
            rows.append([c.get(h, "") for h in CSV_HEADERS])

        start_row = i + 2  # row 1 = headers
        cell_range = f"A{start_row}"
        sheet.update(cell_range, rows)
        total_written += len(chunk)
        print(f"  Written {total_written}/{len(companies)} rows to Sheets...")

        # Throttle to avoid Google API rate limits
        if i + CHUNK_SIZE < len(companies):
            time.sleep(2)

    print(f"\nGoogle Sheets updated: '{sheet_name}' ({total_written} rows)")
    print(f"  Spreadsheet: https://docs.google.com/spreadsheets/d/{spreadsheet_id}")

    return sheet_name


# ---------------------------------------------------------------------------
# Dry run (test mode — no API calls)
# ---------------------------------------------------------------------------

def dry_run(budget: float):
    """Show what the scrape would do without making API calls."""
    total_items = int(budget * 1000)

    print(f"\n{'='*60}")
    print(f"DRY RUN — Bulk Apify Scrape Plan")
    print(f"{'='*60}")
    print(f"Budget: ${budget:.2f} (~{total_items:,} jobs)")
    print(f"Job queries: {', '.join(JOB_QUERIES)}")
    print(f"\nLocation batches:")

    planned_total = 0
    for batch in LOCATION_BATCHES:
        # Scale batch sizes to actual budget
        scale = budget / 20.0
        items = int(batch["max_items"] * scale)
        cost = items * COST_PER_1000 / 1000
        planned_total += items
        print(f"  {batch['location']:25s}  {items:>6,} items  ~${cost:.2f}")

    print(f"\n  {'TOTAL':25s}  {planned_total:>6,} items  ~${planned_total * COST_PER_1000 / 1000:.2f}")
    print(f"\nAfter B2B filtering, expect ~30-50% to pass → {int(planned_total * 0.35):,}-{int(planned_total * 0.5):,} B2B companies")
    print(f"\nTo run for real: remove --dry-run flag")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Bulk Apify Scrape — burn remaining credits on global SDR/BDR hiring data"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Show plan without making API calls")
    parser.add_argument("--budget", type=float, default=20.0,
                        help="Budget in USD (default: $20)")
    parser.add_argument("--csv-only", action="store_true",
                        help="Save to CSV only, skip Google Sheets")
    parser.add_argument("--output", type=str, default=None,
                        help="CSV output path (default: data/bulk_scrape_YYYYMMDD.csv)")
    args = parser.parse_args()

    print(f"\n{'='*60}")
    print(f"Bulk Apify Scrape — Global B2B SDR/BDR Hiring Signals")
    print(f"{'='*60}")
    print(f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"Budget: ${args.budget:.2f} (~{int(args.budget * 1000):,} jobs)")

    if args.dry_run:
        dry_run(args.budget)
        return

    # Load API key
    env_file = Path(__file__).parent.parent / ".env"
    if env_file.exists():
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    os.environ.setdefault(key.strip(), value.strip())

    api_key = os.environ.get("APIFY_API_KEY")
    if not api_key:
        print("ERROR: APIFY_API_KEY not set. Check your .env file.")
        sys.exit(1)

    client = BulkApifyClient(api_key)

    # Check balance first
    print("\nChecking Apify balance...")
    balance = client.check_balance()
    if "error" in balance:
        print(f"  WARNING: Could not check balance: {balance['error']}")
    else:
        print(f"  Account: {balance.get('username', 'N/A')}")
        print(f"  Plan: {balance.get('plan', 'N/A')}")
        print(f"  Estimated remaining: ${balance.get('remaining_usd', '?')}")

    # Scale batches to budget
    scale = args.budget / 20.0
    batches = []
    for batch in LOCATION_BATCHES:
        batches.append({
            "location": batch["location"],
            "max_items": max(100, int(batch["max_items"] * scale)),
        })

    # Run all searches
    print(f"\nStarting {len(batches)} location searches...\n")
    all_jobs = []

    for i, batch in enumerate(batches, 1):
        print(f"[{i}/{len(batches)}] {batch['location']}")
        jobs = client.run_search(batch["location"], batch["max_items"])
        all_jobs.extend(jobs)

        # Safety: stop if we've exceeded budget
        if client.estimated_cost > args.budget * 1.1:
            print(f"\n  Budget exceeded (${client.estimated_cost:.2f} > ${args.budget:.2f}) — stopping.")
            break

    print(f"\n{'='*60}")
    print(f"Scrape Complete")
    print(f"{'='*60}")
    print(f"  Runs:           {client.total_runs}")
    print(f"  Total raw jobs: {client.total_items_fetched:,}")
    print(f"  Estimated cost: ${client.estimated_cost:.2f}")

    # Process: dedup + B2B filter
    print(f"\nProcessing & filtering...")
    companies = process_jobs(all_jobs)

    if not companies:
        print("No B2B companies found. Exiting.")
        return

    # Save CSV
    data_dir = Path(__file__).parent.parent / "data"
    data_dir.mkdir(exist_ok=True)

    csv_path = args.output or str(
        data_dir / f"bulk_scrape_{datetime.now().strftime('%Y%m%d')}.csv"
    )
    save_to_csv(companies, csv_path)

    # Also save raw JSON for backup
    raw_path = str(data_dir / f"bulk_scrape_raw_{datetime.now().strftime('%Y%m%d')}.json")
    with open(raw_path, "w") as f:
        json.dump(all_jobs, f, default=str)
    print(f"Raw JSON backup: {raw_path}")

    # Push to Google Sheets
    if not args.csv_only:
        print(f"\nPushing to Google Sheets...")
        config_path = str(Path(__file__).parent.parent / "config" / "settings.yaml")
        try:
            sheet_name = push_to_sheets(companies, config_path)
            print(f"\nDone! Check the '{sheet_name}' tab in your CRM spreadsheet.")
        except Exception as e:
            print(f"\nERROR pushing to Sheets: {e}")
            print(f"Data is safe in CSV: {csv_path}")

    # Final summary
    print(f"\n{'='*60}")
    print(f"FINAL SUMMARY")
    print(f"{'='*60}")
    print(f"  Apify cost:        ~${client.estimated_cost:.2f}")
    print(f"  Raw jobs scraped:  {client.total_items_fetched:,}")
    print(f"  B2B companies:     {len(companies):,}")
    print(f"  CSV:               {csv_path}")

    # Breakdown by country
    by_country = {}
    for c in companies:
        country = c.get("country") or c.get("country_code") or "Unknown"
        by_country[country] = by_country.get(country, 0) + 1

    print(f"\n  By country:")
    for country, count in sorted(by_country.items(), key=lambda x: -x[1]):
        print(f"    {country:25s}  {count:>5,}")


if __name__ == "__main__":
    main()
