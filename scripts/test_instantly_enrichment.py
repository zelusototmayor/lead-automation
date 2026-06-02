"""
Test Instantly SuperSearch Enrichment
======================================
Small-batch test to validate Instantly's lead finder as an Apollo replacement.
Searches for decision-makers at B2B companies hiring SDRs, gets verified emails.

Credit cost: 1-4 credits per verified email (0 if not found).
With 1,000 credits → ~250-1,000 enriched contacts.

Usage:
    python scripts/test_instantly_enrichment.py                # Run test (10 leads)
    python scripts/test_instantly_enrichment.py --limit 5      # Fewer leads
    python scripts/test_instantly_enrichment.py --dry-run      # Check API access only
"""

import os
import sys
import json
import time
import argparse
import requests
from pathlib import Path
from datetime import datetime

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

INSTANTLY_API_BASE = "https://api.instantly.ai/api/v2"

# Test companies — mix of known B2B SaaS companies hiring SDRs in target regions
# These simulate what we'd get from the Apify bulk scrape
TEST_COMPANIES = [
    # EU companies
    {"company": "Pipedrive",     "country": "United Kingdom", "job_title": "SDR"},
    {"company": "Typeform",      "country": "Spain",          "job_title": "SDR"},
    {"company": "Unbabel",       "country": "Portugal",       "job_title": "BDR"},
    {"company": "Talkdesk",      "country": "Portugal",       "job_title": "SDR"},
    {"company": "OutSystems",    "country": "Portugal",       "job_title": "Sales Development Representative"},
    # US companies
    {"company": "Gong",          "country": "United States",  "job_title": "SDR"},
    {"company": "Outreach",      "country": "United States",  "job_title": "SDR"},
    {"company": "Salesloft",     "country": "United States",  "job_title": "BDR"},
    # Other English-speaking
    {"company": "Canva",         "country": "Australia",      "job_title": "SDR"},
    {"company": "Shopify",       "country": "Canada",         "job_title": "BDR"},
]

# ICP seniority filters — we want decision-makers, not the SDRs themselves
TARGET_TITLES = [
    "VP of Sales",
    "Head of Sales",
    "Director of Sales",
    "Chief Revenue Officer",
    "VP Sales",
    "Sales Director",
    "Head of Growth",
    "Founder",
    "CEO",
    "CRO",
]

TARGET_SENIORITIES = ["vp", "director", "c_suite", "owner", "founder"]


# ---------------------------------------------------------------------------
# Instantly API helpers
# ---------------------------------------------------------------------------

class InstantlyEnrichmentClient:
    """Client for Instantly SuperSearch enrichment API."""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        self.credits_used = 0

    def check_account(self) -> dict:
        """Check account status and available features."""
        try:
            # Try workspace endpoint to verify API access
            resp = requests.get(
                f"{INSTANTLY_API_BASE}/workspace",
                headers=self.headers,
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            return {"status": "ok", "data": data}
        except requests.RequestException as e:
            return {"status": "error", "error": str(e)}

    def supersearch_enrich(
        self,
        search_filters: dict,
        limit: int = 10,
        resource_id: str = None,
    ) -> dict:
        """
        Search and enrich leads from Instantly's SuperSearch database.

        POST /api/v2/supersearch-enrichment/enrich-leads-from-supersearch

        Args:
            search_filters: Filters like job titles, company, location, etc.
            limit: Max leads to return.
            resource_id: Optional list/campaign ID to add leads to.

        Returns:
            API response with enriched leads.
        """
        url = f"{INSTANTLY_API_BASE}/supersearch-enrichment/enrich-leads-from-supersearch"

        payload = {
            "search_filters": search_filters,
            "limit": limit,
        }

        if resource_id:
            payload["resource_id"] = resource_id

        try:
            resp = requests.post(
                url,
                headers=self.headers,
                json=payload,
                timeout=60,
            )
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            status_code = getattr(e.response, 'status_code', None) if hasattr(e, 'response') else None
            body = ""
            if hasattr(e, 'response') and e.response is not None:
                try:
                    body = e.response.text[:500]
                except Exception:
                    pass
            return {
                "error": str(e),
                "status_code": status_code,
                "body": body,
            }

    def search_people(
        self,
        company_name: str = None,
        job_titles: list[str] = None,
        locations: list[str] = None,
        seniority: list[str] = None,
        limit: int = 1,
        one_per_company: bool = True,
    ) -> dict:
        """
        High-level search: find decision-makers at a specific company.

        Builds search_filters and calls supersearch_enrich.
        """
        filters = {}

        if job_titles:
            filters["job_titles"] = job_titles

        if company_name:
            filters["company_name"] = [company_name]

        if locations:
            filters["locations"] = {"include": locations}

        if seniority:
            filters["seniority"] = seniority

        if one_per_company:
            filters["one_per_company"] = True

        return self.supersearch_enrich(
            search_filters=filters,
            limit=limit,
        )

    def get_enrichment_status(self, enrichment_id: str) -> dict:
        """Check status of an enrichment job."""
        url = f"{INSTANTLY_API_BASE}/supersearch-enrichment/{enrichment_id}"
        try:
            resp = requests.get(url, headers=self.headers, timeout=15)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            return {"error": str(e)}


# ---------------------------------------------------------------------------
# Test runner
# ---------------------------------------------------------------------------

def run_test(api_key: str, limit: int = 10, dry_run: bool = False):
    """Run a small enrichment test."""
    client = InstantlyEnrichmentClient(api_key)

    print(f"\n{'='*60}")
    print(f"Instantly SuperSearch Enrichment Test")
    print(f"{'='*60}")
    print(f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"Test leads: {min(limit, len(TEST_COMPANIES))}")

    # Step 1: Check API access
    print(f"\n--- Step 1: Checking API access ---")
    account = client.check_account()
    if account["status"] == "error":
        print(f"  ERROR: {account['error']}")
        print(f"  Make sure INSTANTLY_API_KEY is a V2 key (Bearer token).")
        return
    print(f"  API access: OK")
    print(f"  Workspace data: {json.dumps(account.get('data', {}), indent=2)[:300]}")

    if dry_run:
        print(f"\n--- DRY RUN: Would test {min(limit, len(TEST_COMPANIES))} companies ---")
        for c in TEST_COMPANIES[:limit]:
            print(f"  {c['company']} ({c['country']}) — looking for: {', '.join(TARGET_TITLES[:3])}...")
        print(f"\n  Estimated credits: {min(limit, len(TEST_COMPANIES)) * 1}-{min(limit, len(TEST_COMPANIES)) * 4}")
        print(f"  To run for real: remove --dry-run")
        return

    # Step 2: Test individual company searches
    print(f"\n--- Step 2: Testing individual company enrichment ---")
    print(f"  Looking for decision-makers (VP/Director/C-suite) at test companies...\n")

    results = []
    companies_to_test = TEST_COMPANIES[:limit]

    for i, company_info in enumerate(companies_to_test, 1):
        company = company_info["company"]
        country = company_info["country"]

        print(f"  [{i}/{len(companies_to_test)}] {company} ({country})...")

        result = client.search_people(
            company_name=company,
            job_titles=TARGET_TITLES,
            locations=[country],
            seniority=TARGET_SENIORITIES,
            limit=1,
            one_per_company=True,
        )

        if "error" in result:
            print(f"    ERROR: {result['error']}")
            print(f"    Status: {result.get('status_code')}")
            print(f"    Body: {result.get('body', '')[:200]}")
            results.append({
                "company": company,
                "country": country,
                "status": "error",
                "error": result["error"],
            })
        else:
            # Parse response
            leads = result.get("leads", result.get("items", result.get("data", [])))
            if not isinstance(leads, list):
                leads = [result] if result.get("email") else []

            if leads:
                lead = leads[0] if leads else {}
                email = lead.get("email", lead.get("work_email", ""))
                name = lead.get("name", lead.get("full_name", ""))
                title = lead.get("title", lead.get("job_title", ""))
                phone = lead.get("phone", lead.get("phone_number", ""))
                linkedin = lead.get("linkedin_url", lead.get("linkedin", ""))

                print(f"    FOUND: {name} | {title}")
                print(f"    Email: {email}")
                if phone:
                    print(f"    Phone: {phone}")

                results.append({
                    "company": company,
                    "country": country,
                    "status": "found",
                    "name": name,
                    "title": title,
                    "email": email,
                    "phone": phone,
                    "linkedin": linkedin,
                    "raw_fields": list(lead.keys()),
                })
            else:
                print(f"    No leads found")
                results.append({
                    "company": company,
                    "country": country,
                    "status": "not_found",
                    "raw_response_keys": list(result.keys()),
                    "raw_response_preview": json.dumps(result, default=str)[:300],
                })

        # Rate limit — be nice to the API
        time.sleep(2)

    # Step 3: Also try a broader ICP-based search (not company-specific)
    print(f"\n--- Step 3: Testing ICP-based search (no specific company) ---")
    print(f"  Searching for VP/Director of Sales at B2B SaaS companies in Portugal...\n")

    icp_result = client.supersearch_enrich(
        search_filters={
            "job_titles": ["VP of Sales", "Head of Sales", "Sales Director", "CRO"],
            "locations": {"include": ["Portugal"]},
            "seniority": ["vp", "director", "c_suite"],
            "industry": ["computer software", "information technology & services", "internet"],
            "employee_count": {"min": 10, "max": 200},
            "one_per_company": True,
        },
        limit=5,
    )

    if "error" in icp_result:
        print(f"  ERROR: {icp_result['error']}")
        print(f"  Status: {icp_result.get('status_code')}")
        print(f"  Body: {icp_result.get('body', '')[:300]}")
    else:
        icp_leads = icp_result.get("leads", icp_result.get("items", icp_result.get("data", [])))
        if isinstance(icp_leads, list) and icp_leads:
            for lead in icp_leads:
                name = lead.get("name", lead.get("full_name", ""))
                title = lead.get("title", lead.get("job_title", ""))
                company = lead.get("company_name", lead.get("company", ""))
                email = lead.get("email", lead.get("work_email", ""))
                print(f"  {name} | {title} @ {company} | {email}")
        else:
            print(f"  Raw response keys: {list(icp_result.keys())}")
            print(f"  Response preview: {json.dumps(icp_result, default=str)[:400]}")

    # Summary
    print(f"\n{'='*60}")
    print(f"TEST RESULTS SUMMARY")
    print(f"{'='*60}")

    found = [r for r in results if r["status"] == "found"]
    not_found = [r for r in results if r["status"] == "not_found"]
    errors = [r for r in results if r["status"] == "error"]

    print(f"  Companies tested:  {len(results)}")
    print(f"  Contacts found:    {len(found)}")
    print(f"  Not found:         {len(not_found)}")
    print(f"  Errors:            {len(errors)}")

    if found:
        print(f"\n  Contacts found:")
        for r in found:
            print(f"    {r['company']:20s} → {r['name']} ({r['title']}) | {r['email']}")
            print(f"      Fields available: {', '.join(r['raw_fields'][:10])}")

    if not_found:
        print(f"\n  Not found (check raw response for debugging):")
        for r in not_found:
            print(f"    {r['company']:20s} → keys: {r['raw_response_keys']}")

    if errors:
        print(f"\n  Errors:")
        for r in errors:
            print(f"    {r['company']:20s} → {r['error'][:100]}")

    # Save full results for analysis
    output_dir = Path(__file__).parent.parent / "data"
    output_dir.mkdir(exist_ok=True)
    output_file = output_dir / f"instantly_enrichment_test_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
    with open(output_file, "w") as f:
        json.dump({
            "test_date": datetime.now().isoformat(),
            "results": results,
            "icp_search_response": icp_result if "error" not in (icp_result or {}) else {"error": str(icp_result)},
        }, f, indent=2, default=str)
    print(f"\n  Full results saved: {output_file}")

    # Recommendation
    hit_rate = len(found) / len(results) * 100 if results else 0
    print(f"\n--- RECOMMENDATION ---")
    if hit_rate >= 60:
        credits_needed = 1000  # user's available credits
        estimated_leads = int(credits_needed / 2)  # ~2 credits avg per lead
        print(f"  Hit rate: {hit_rate:.0f}% — GOOD. Instantly enrichment is viable.")
        print(f"  With 1,000 credits → ~{estimated_leads} enriched leads")
        print(f"  Next step: Run bulk_apify_scrape.py on Apr 8, then enrich via Instantly.")
    elif hit_rate >= 30:
        print(f"  Hit rate: {hit_rate:.0f}% — MODERATE. Worth using but expect gaps.")
        print(f"  Consider supplementing with LinkedIn manual outreach for misses.")
    else:
        print(f"  Hit rate: {hit_rate:.0f}% — LOW. May need to adjust search filters.")
        print(f"  Check if the API response format matches what we're parsing.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Test Instantly SuperSearch enrichment (Apollo replacement)"
    )
    parser.add_argument("--limit", type=int, default=10,
                        help="Number of test companies (default: 10)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Check API access only, no enrichment calls")
    args = parser.parse_args()

    # Load API key
    env_file = Path(__file__).parent.parent / ".env"
    if env_file.exists():
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    os.environ.setdefault(key.strip(), value.strip())

    api_key = os.environ.get("INSTANTLY_API_KEY")
    if not api_key:
        print("ERROR: INSTANTLY_API_KEY not set. Check your .env file.")
        sys.exit(1)

    run_test(api_key, limit=args.limit, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
