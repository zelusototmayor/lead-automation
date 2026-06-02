"""
LinkedIn Outbound — Prospect Finder
=====================================
Uses browser automation to find decision-maker LinkedIn profiles
from company LinkedIn pages. Reads companies from the Apify bulk
scrape sheet, finds the right person, stores in LinkedIn Outbound tab.

Designed to run as a Cowork scheduled task with Claude in Chrome.
Can also be invoked as a standalone script for the CRM logic.

Usage (standalone — CRM logic only, no browser):
    python -m src.linkedin.prospect_finder --load-from-apify --limit 25
"""

import os
import sys
import argparse
import random
import time
from pathlib import Path
from datetime import datetime
import structlog

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.linkedin.config import (
    SELECTION_CRITERIA,
    SKIP_TITLE_KEYWORDS,
    SKIP_INDICATORS,
    get_tier,
)
from src.linkedin.sheets_crm import LinkedInSheetsCRM

logger = structlog.get_logger()


def load_config():
    """Load config and env vars."""
    import yaml
    env_file = Path(__file__).parent.parent.parent / ".env"
    if env_file.exists():
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    os.environ.setdefault(key.strip(), value.strip())

    config_path = Path(__file__).parent.parent.parent / "config" / "settings.yaml"
    with open(config_path) as f:
        config = yaml.safe_load(f)

    def replace_env_vars(obj):
        if isinstance(obj, str) and obj.startswith("${") and obj.endswith("}"):
            return os.environ.get(obj[2:-1], "")
        elif isinstance(obj, dict):
            return {k: replace_env_vars(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [replace_env_vars(item) for item in obj]
        return obj

    return replace_env_vars(config)


def load_apify_companies(config: dict, limit: int = 25) -> list[dict]:
    """Load unprocessed companies from the Apify Bulk scrape sheet.

    Reads the most recent 'Apify Bulk *' tab from the CRM spreadsheet,
    returns companies not already in the LinkedIn Outbound tab.
    """
    import gspread
    from google.oauth2.service_account import Credentials

    creds = Credentials.from_service_account_file(
        config["google_sheets"]["credentials_file"],
        scopes=["https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive"],
    )
    gc = gspread.authorize(creds)
    spreadsheet = gc.open_by_key(config["google_sheets"]["spreadsheet_id"])

    # Find the Apify Bulk sheet (most recent)
    apify_sheets = [
        ws for ws in spreadsheet.worksheets()
        if ws.title.startswith("Apify Bulk")
    ]
    if not apify_sheets:
        logger.error("No 'Apify Bulk' sheet found")
        return []

    # Sort by title (date is in the name) — most recent last
    apify_sheet = sorted(apify_sheets, key=lambda ws: ws.title)[-1]
    logger.info("Loading from Apify sheet", sheet=apify_sheet.title)

    all_rows = apify_sheet.get_all_records()
    logger.info("Apify sheet rows", count=len(all_rows))

    # Get existing companies from LinkedIn Outbound tab for dedup
    crm = LinkedInSheetsCRM(
        credentials_file=config["google_sheets"]["credentials_file"],
        spreadsheet_id=config["google_sheets"]["spreadsheet_id"],
    )
    existing_urls = crm.get_all_linkedin_urls()

    companies = []
    for row in all_rows:
        company_linkedin = row.get("Company LinkedIn", row.get("company_linkedin_url", ""))
        company_name = row.get("Company", row.get("company_name", ""))

        if not company_name:
            continue

        # Skip if already in LinkedIn Outbound
        if company_linkedin and company_linkedin.lower() in existing_urls:
            continue

        companies.append({
            "company": company_name,
            "company_linkedin": company_linkedin,
            "job_hiring": row.get("Job Title", row.get("job_title", "")),
            "country": row.get("Country", row.get("country", "")),
            "city": row.get("City", row.get("city", "")),
            "description_snippet": row.get("Description (snippet)", row.get("description_snippet", "")),
        })

        if len(companies) >= limit:
            break

    # Shuffle to get variety across countries
    random.shuffle(companies)
    logger.info("Companies to process", count=len(companies))
    return companies[:limit]


def should_skip_prospect(title: str, headline: str = "") -> bool:
    """Check if a prospect should be skipped based on their title/headline."""
    text = (title + " " + headline).lower()

    for skip in SKIP_TITLE_KEYWORDS:
        if skip in text:
            return True

    for indicator in SKIP_INDICATORS:
        if indicator in text:
            return True

    return False


def get_search_titles(employee_count: int) -> list[list[str]]:
    """Get the title search groups for a given company size."""
    tier = get_tier(employee_count)
    criteria = SELECTION_CRITERIA[tier]
    return criteria["title_searches"]


def get_browser_instructions(company: dict) -> str:
    """Generate browser automation instructions for finding a prospect.

    This returns a prompt that Claude in Chrome can execute.
    """
    company_url = company.get("company_linkedin", "")
    company_name = company.get("company", "")
    employee_count = company.get("employee_count") or 0

    try:
        employee_count = int(str(employee_count).replace(",", "").split("-")[0])
    except (ValueError, TypeError):
        employee_count = 0

    tier = get_tier(employee_count)
    search_groups = SELECTION_CRITERIA[tier]["title_searches"]

    # Flatten title searches for the prompt
    all_titles = []
    for group in search_groups:
        all_titles.extend(group)

    instructions = f"""Find the right decision-maker at {company_name} on LinkedIn.

1. Go to {company_url}/people/ (the company's People page)
   - If no company URL, search LinkedIn for "{company_name}" and navigate to their company page → People tab

2. In the search/filter on the People page, search for these titles in order of priority:
   {', '.join(all_titles[:4])}

3. From the results, pick the FIRST person who:
   - Has a sales, revenue, growth, or business development related title
   - Has been at the company for 6+ months (check their profile dates)
   - Has a profile photo and recent activity
   - Is NOT a recruiter, HR person, or intern

4. If no match on primary titles, try the next group: {', '.join(all_titles[4:]) if len(all_titles) > 4 else 'skip'}

5. Once you find the right person, extract:
   - Full name
   - Job title
   - LinkedIn profile URL
   - Employee count (from the company page header)

6. If the company page shows no employees or the People tab is empty, skip this company.

Company size tier: {tier} ({"under 20" if tier == "small" else "20-50" if tier == "medium" else "50+"} employees)
Country: {company.get('country', 'unknown')}
"""
    return instructions


def add_prospects_to_crm(
    config: dict,
    prospects: list[dict],
) -> int:
    """Add found prospects to the LinkedIn Outbound CRM tab."""
    crm = LinkedInSheetsCRM(
        credentials_file=config["google_sheets"]["credentials_file"],
        spreadsheet_id=config["google_sheets"]["spreadsheet_id"],
    )

    added = 0
    for prospect in prospects:
        lead_id = crm.add_prospect(prospect)
        if lead_id:
            added += 1

    logger.info("Prospects added to CRM", added=added, total=len(prospects))
    return added


def main():
    """Standalone mode: load companies from Apify, print browser instructions."""
    parser = argparse.ArgumentParser(description="LinkedIn Prospect Finder")
    parser.add_argument("--load-from-apify", action="store_true",
                        help="Load companies from Apify Bulk sheet")
    parser.add_argument("--limit", type=int, default=25,
                        help="Max companies to process")
    parser.add_argument("--print-instructions", action="store_true",
                        help="Print browser instructions for each company")
    args = parser.parse_args()

    config = load_config()

    if args.load_from_apify:
        companies = load_apify_companies(config, limit=args.limit)
        print(f"\nLoaded {len(companies)} companies from Apify sheet")

        if args.print_instructions:
            for i, company in enumerate(companies, 1):
                print(f"\n{'='*60}")
                print(f"[{i}/{len(companies)}] {company['company']}")
                print(f"{'='*60}")
                print(get_browser_instructions(company))
        else:
            for c in companies:
                print(f"  {c['company']:40s} | {c['country']:15s} | {c['job_hiring']}")


if __name__ == "__main__":
    main()
