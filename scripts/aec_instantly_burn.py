"""
AEC Pipeline — Instantly Credit Burn (1,000 credits)
=====================================================
One-shot script to burn all remaining Instantly credits on AEC leads.
Replaces Apollo enrichment with Instantly SuperSearch.

Flow: Google Maps (find AEC firms) → Instantly SuperSearch (get email)
      → Google Sheets CRM → optionally personalize + queue in Instantly

No Apollo credits used. Estimated output: ~300-500 enriched AEC leads.

Usage:
    python scripts/aec_instantly_burn.py                   # Full run
    python scripts/aec_instantly_burn.py --dry-run         # Plan only
    python scripts/aec_instantly_burn.py --source-only     # Skip personalization
    python scripts/aec_instantly_burn.py --credit-budget 200  # Limit credits
"""

import os
import sys
import random
import argparse
from pathlib import Path
from datetime import datetime
import structlog

sys.path.insert(0, str(Path(__file__).parent.parent))

structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
)
logger = structlog.get_logger()


def _load_dotenv():
    env_file = Path(__file__).parent.parent / ".env"
    if env_file.exists():
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    os.environ.setdefault(key.strip(), value.strip())


def load_config():
    import yaml
    _load_dotenv()
    config_path = Path(__file__).parent.parent / "config" / "settings.yaml"
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


def dry_run(config: dict, credit_budget: int):
    """Show what the burn would do without making API calls."""
    cities = config["lead_sourcing"]["target_cities"]
    queries = config["lead_sourcing"]["search_queries"]

    # Estimate: ~5 companies per query per city, ~50% enrichment success
    companies_per_city = len(queries) * 5
    total_companies = len(cities) * companies_per_city
    # At ~2 credits per enrichment attempt, ~50% success rate
    estimated_enrichments = credit_budget // 2
    estimated_leads = int(estimated_enrichments * 0.5)

    print(f"\n{'='*60}")
    print(f"DRY RUN — AEC Instantly Credit Burn")
    print(f"{'='*60}")
    print(f"Credit budget: {credit_budget}")
    print(f"Cities: {len(cities)}")
    for c in cities:
        print(f"  - {c['name']}, {c['country']}")
    print(f"Search queries: {len(queries)}")
    for q in queries:
        print(f"  - {q}")
    print(f"\nEstimates:")
    print(f"  Companies to source (Google Maps): ~{total_companies}")
    print(f"  Enrichment attempts (Instantly):   ~{estimated_enrichments}")
    print(f"  Leads with email:                  ~{estimated_leads}")
    print(f"  Credits per lead:                  ~2-4")
    print(f"\nTo run for real: remove --dry-run")


def run_burn(config: dict, credit_budget: int, source_only: bool = False):
    """Run the full AEC credit burn."""
    from src.lead_sourcing import search_agencies
    from src.lead_sourcing.instantly_enrichment import InstantlyEnrichmentClient
    from src.crm import GoogleSheetsCRM
    from src.outreach import EmailPersonalizer, calculate_lead_score, InstantlyClient

    cities = config["lead_sourcing"]["target_cities"]
    queries = config["lead_sourcing"]["search_queries"]
    exclude_keywords = config["lead_sourcing"]["exclude_keywords"]
    google_maps_key = config["api_keys"]["google_maps"]
    instantly_key = config.get("instantly", {}).get("api_key", "")

    # Initialize Instantly enrichment client
    enrichment_client = InstantlyEnrichmentClient(
        api_key=instantly_key,
        credit_budget=credit_budget,
    )

    # Initialize CRM
    crm = GoogleSheetsCRM(
        credentials_file=config["google_sheets"]["credentials_file"],
        spreadsheet_id=config["google_sheets"]["spreadsheet_id"],
        sheet_name=config["google_sheets"]["sheet_name"],
    )

    # Get existing emails for dedup
    existing_emails = crm.get_all_emails()
    existing_companies = set()
    try:
        leads = crm.get_leads_for_outreach(limit=2000)
        existing_companies = set(
            (l.get("company") or "").strip().lower()
            for l in leads if l.get("company")
        )
    except Exception:
        pass

    logger.info("Starting AEC credit burn",
                credit_budget=credit_budget,
                existing_emails=len(existing_emails),
                existing_companies=len(existing_companies),
                cities=len(cities),
                queries=len(queries))

    print(f"\n{'='*60}")
    print(f"AEC Pipeline — Instantly Credit Burn")
    print(f"{'='*60}")
    print(f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"Credit budget: {credit_budget}")
    print(f"Existing leads: {len(existing_emails)} emails, {len(existing_companies)} companies")

    # Shuffle cities for variety
    random.shuffle(cities)

    new_leads = []
    seen_domains = set()
    total_sourced = 0
    total_enrichment_attempts = 0
    skipped_dup = 0
    skipped_no_website = 0
    skipped_no_email = 0
    errors = 0

    for city_idx, city_info in enumerate(cities, 1):
        city = city_info["name"]
        country = city_info["country"]

        # Stop if credits exhausted
        if enrichment_client._credits_exhausted:
            print(f"\n  Credits exhausted — stopping.")
            break

        print(f"\n[{city_idx}/{len(cities)}] {city}, {country}")

        # Use more queries per city since we're doing a bulk burn
        queries_to_use = queries  # Use ALL queries, not a sample

        agencies = search_agencies(
            api_key=google_maps_key,
            city=city,
            country=country,
            search_queries=queries_to_use,
            max_per_query=10,  # Increased from 5 for bulk
            exclude_keywords=exclude_keywords,
        )

        total_sourced += len(agencies)
        print(f"  Found {len(agencies)} companies via Google Maps")

        for agency in agencies:
            # Budget check
            if enrichment_client._credits_exhausted:
                break

            # Skip if no website
            if not agency.get("website"):
                skipped_no_website += 1
                continue

            # Domain dedup
            domain = (agency["website"].replace("https://", "")
                      .replace("http://", "").replace("www.", "")
                      .split("/")[0].lower())
            if domain in seen_domains:
                skipped_dup += 1
                continue
            seen_domains.add(domain)

            # Company dedup
            company_lower = agency["name"].strip().lower()
            if company_lower in existing_companies:
                skipped_dup += 1
                continue

            # Enrich via Instantly SuperSearch
            total_enrichment_attempts += 1
            contact = enrichment_client.find_contact_at_company(
                company_name=agency["name"],
                website=agency.get("website"),
                city=city,
                country=country,
            )

            if not contact or not contact.get("email"):
                skipped_no_email += 1
                continue

            email = contact["email"]

            # Email dedup
            if email.lower() in existing_emails:
                skipped_dup += 1
                continue

            # Build lead data (same format as main.py)
            lead_data = {
                "company": agency["name"],
                "contact_name": contact.get("full_name", ""),
                "email": email,
                "phone": contact.get("phone", "") or agency.get("phone", ""),
                "website": agency.get("website", ""),
                "industry": contact.get("industry", ""),
                "employee_count": str(contact.get("employee_count", "")),
                "city": city,
                "country": country,
                "linkedin": contact.get("linkedin_url", ""),
                "title": contact.get("title", ""),
                "source": "google_maps + instantly_supersearch",
                "description": "",
                "notes": f"Enriched via Instantly SuperSearch credit burn {datetime.now().strftime('%Y-%m-%d')}",
            }

            lead_data["lead_score"] = calculate_lead_score(lead_data)

            # Add to CRM
            try:
                lead_id = crm.add_lead(lead_data)
                if lead_id:
                    lead_data["id"] = lead_id
                    new_leads.append(lead_data)
                    existing_emails.add(email.lower())
                    existing_companies.add(company_lower)

                    print(f"  + {agency['name']:40s} | {contact.get('full_name', ''):25s} | {email}")
                else:
                    skipped_dup += 1
            except Exception as e:
                logger.error("Error adding lead", company=agency["name"], error=str(e))
                errors += 1

    # Personalize and queue (unless source_only)
    queued = 0
    if not source_only and new_leads:
        print(f"\nPersonalizing and queuing {len(new_leads)} leads...")
        try:
            import yaml
            templates_path = Path(__file__).parent.parent / "config" / "email_templates.yaml"
            with open(templates_path) as f:
                templates = yaml.safe_load(f)

            personalizer = EmailPersonalizer(
                api_key=config["api_keys"]["anthropic"],
                model=config.get("personalization", {}).get("model", "claude-sonnet-4-20250514"),
            )

            instantly_client = InstantlyClient(api_key=instantly_key)
            campaign_name = config.get("instantly", {}).get("campaign_name", "AEC Business Development")
            campaigns = instantly_client.list_campaigns()
            campaign_id = None
            for c in campaigns:
                if c.get("name") == campaign_name:
                    campaign_id = c.get("id")
                    break

            if campaign_id:
                sequence = templates["sequences"]["default"]["emails"][0]
                template = sequence["body_template"]
                sender_info = {
                    "bio": config["personalization"]["sender_bio"],
                    "value_proposition": config["personalization"]["value_proposition"],
                    "aec_verticals": templates.get("aec_verticals"),
                }

                personalized = []
                for lead in new_leads:
                    try:
                        p = personalizer.personalize_email(
                            lead=lead, template=template, sender_info=sender_info
                        )
                        if p:
                            lead.update({
                                "personalized_opener": p.get("personalized_opener", ""),
                                "specific_pain_point": p.get("specific_pain_point", ""),
                                "industry_specific_insight": p.get("industry_specific_insight", ""),
                                "suggested_subject": p.get("suggested_subject", ""),
                                "first_name": lead.get("contact_name", "").split()[0]
                                              if lead.get("contact_name") else "",
                            })
                            personalized.append(lead)
                    except Exception as e:
                        logger.error("Personalization failed", company=lead.get("company"), error=str(e))

                if personalized:
                    result = instantly_client.add_leads_to_campaign(campaign_id, personalized)
                    if result:
                        queued = len(personalized)
                        for lead in personalized:
                            crm.update_lead(lead["id"], {"status": "Queued"})
                        print(f"  Queued {queued} leads in Instantly campaign '{campaign_name}'")
            else:
                print(f"  Campaign '{campaign_name}' not found — skipping queue")

        except Exception as e:
            logger.error("Personalization/queue step failed", error=str(e))
            print(f"  Personalization failed: {e}")
            print(f"  Leads are saved in CRM — you can personalize later.")

    # Final summary
    credit_summary = enrichment_client.get_credit_summary()

    print(f"\n{'='*60}")
    print(f"BURN COMPLETE")
    print(f"{'='*60}")
    print(f"  Companies sourced (Google Maps):  {total_sourced}")
    print(f"  Enrichment attempts (Instantly):  {total_enrichment_attempts}")
    print(f"  Leads added to CRM:               {len(new_leads)}")
    print(f"  Queued in Instantly:               {queued}")
    print(f"  Skipped (duplicate):               {skipped_dup}")
    print(f"  Skipped (no website):              {skipped_no_website}")
    print(f"  Skipped (no email):                {skipped_no_email}")
    print(f"  Errors:                            {errors}")
    print(f"\n  Instantly credits used (est):      ~{credit_summary['credits_used']}")
    print(f"  Credits per lead:                  ~{credit_summary['credits_per_lead']}")

    stats = crm.get_stats()
    print(f"\n  Total leads in CRM:                {stats['total_leads']}")

    return {
        "sourced": total_sourced,
        "enriched": len(new_leads),
        "queued": queued,
        "credits_used": credit_summary["credits_used"],
        "credits_per_lead": credit_summary["credits_per_lead"],
    }


def main():
    parser = argparse.ArgumentParser(
        description="AEC Pipeline — Burn 1,000 Instantly credits on AEC leads"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Show plan without making API calls")
    parser.add_argument("--source-only", action="store_true",
                        help="Source + enrich only, skip personalization and Instantly queue")
    parser.add_argument("--credit-budget", type=int, default=1000,
                        help="Max Instantly credits to use (default: 1000)")
    args = parser.parse_args()

    config = load_config()

    if args.dry_run:
        dry_run(config, args.credit_budget)
        return

    run_burn(config, args.credit_budget, source_only=args.source_only)


if __name__ == "__main__":
    main()
