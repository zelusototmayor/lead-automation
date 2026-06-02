"""
Logistics Quote Agent — UK + Spain SMB Pipeline
================================================
Standalone runner for the "Quote Agent" offer sold to small/mid-market
freight brokers, freight forwarders, and 3PLs in the United Kingdom
and Spain.

Flow: Google Maps (UK/ES logistics firms) → Instantly SuperSearch
      (decision-maker email) → Google Sheets CRM → Instantly campaign

Notes:
- No Apollo. SuperSearch is the sole enrichment source.
- No product URLs in email copy (spam risk). `max@zelusottomayor.com`
  is the try-it address.
- Email copy for this ICP lives IN Instantly, not in repo YAML —
  so this pipeline pushes leads to the campaign without applying a
  repo template. Personalization runs only if a template is wired
  in Instantly; otherwise leads are queued with merge fields only.
- Account for EN/ES language mismatch — this pipeline treats UK and
  Spain leads identically at sourcing time; language split happens
  inside the Instantly sequence (separate campaign per language
  is fine if you prefer — see config/settings.yaml).

Usage:
    python src/logistics.py                      # Run with defaults
    python src/logistics.py --target 20          # Override target
    python src/logistics.py --source-only        # Source + enrich only
    python src/logistics.py --sync-only          # Sync engagement only
"""

import os
import sys
import yaml
import random
import argparse
from pathlib import Path
import structlog

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.lead_sourcing import search_agencies
from src.lead_sourcing.instantly_enrichment import InstantlyEnrichmentClient
from src.crm import GoogleSheetsCRM
from src.outreach import EmailPersonalizer, calculate_lead_score, InstantlyClient, InstantlySyncer

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


# Decision-maker targeting for freight / forwarding / 3PL SMBs.
# These firms are often founder-run; ops directors also hold buying power.
LOGISTICS_SENIORITIES = ["owner", "founder", "c_suite", "vp", "director"]
LOGISTICS_TITLES = [
    "Owner", "Founder", "Managing Director", "CEO", "Director",
    "Operations Director", "Head of Operations", "Commercial Director",
    "Sales Director", "Head of Sales",
    # Spanish-language variants (SuperSearch indexes multilingual titles)
    "Director General", "Gerente", "Director Comercial",
    "Director de Operaciones", "Responsable Comercial",
]


def _load_dotenv():
    env_file = Path(__file__).parent.parent / ".env"
    if env_file.exists():
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    os.environ.setdefault(key.strip(), value.strip())


def load_config(config_path: str = "config/settings.yaml") -> dict:
    _load_dotenv()
    with open(config_path, "r") as f:
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


class LogisticsSourcer:
    """Sources SMB freight / forwarding / 3PL firms in UK + Spain."""

    def __init__(self, config: dict):
        self.config = config
        self.lc = config["logistics"]
        self.daily_target = self.lc["daily_target"]
        self.max_per_query = self.lc.get("max_per_query", 5)
        self.regions = self.lc["target_regions"]
        self.queries = self.lc["search_queries"]
        self.exclude_keywords = self.lc.get("exclude_keywords", [])

        # API keys
        self.google_maps_key = config["api_keys"]["google_maps"]
        self.instantly_key = config.get("instantly", {}).get("api_key", "")

        # SuperSearch enrichment client
        supersearch_budget = self.lc.get("supersearch_credit_budget", 100)
        self.enrichment_client = InstantlyEnrichmentClient(
            api_key=self.instantly_key,
            credit_budget=supersearch_budget,
        )

        # CRM — writes to the "Logistics Quote v2" tab
        self.crm = GoogleSheetsCRM(
            credentials_file=config["google_sheets"]["credentials_file"],
            spreadsheet_id=config["google_sheets"]["spreadsheet_id"],
            sheet_name=self.lc.get("sheet_name", "Logistics Quote v2"),
        )

        # Personalizer (optional — only used if a template is supplied)
        self.personalizer = EmailPersonalizer(
            api_key=config["api_keys"]["anthropic"],
            model=config.get("personalization", {}).get("model", "claude-sonnet-4-20250514"),
        )

        # Instantly client for pushing leads
        self.instantly = InstantlyClient(api_key=self.instantly_key)

        logger.info("LogisticsSourcer initialized",
                     target=self.daily_target,
                     regions=len(self.regions),
                     queries=len(self.queries),
                     supersearch_budget=supersearch_budget)

    def run(self, target: int = None, source_only: bool = False) -> dict:
        """Source + enrich + queue logistics leads."""
        target = target or self.daily_target
        logger.info("Starting logistics sourcing", target=target)

        # Existing dedup sets
        existing_emails = self.crm.get_all_emails()
        existing_companies = self._get_existing_companies()
        logger.info("Existing leads in CRM",
                     emails=len(existing_emails),
                     companies=len(existing_companies))

        # Shuffle regions for variety per run
        regions = list(self.regions)
        random.shuffle(regions)

        added = 0
        skipped_dup = 0
        skipped_no_website = 0
        skipped_no_email = 0
        errors = 0
        new_leads = []
        seen_domains = set()

        for region in regions:
            if added >= target or self.enrichment_client._credits_exhausted:
                break

            city = region["name"]
            country = region["country"]
            queries = random.sample(self.queries, min(3, len(self.queries)))

            logger.info("Searching", city=city, country=country, queries=queries)

            try:
                firms = search_agencies(
                    api_key=self.google_maps_key,
                    city=city,
                    country=country,
                    search_queries=queries,
                    max_per_query=self.max_per_query,
                    exclude_keywords=self.exclude_keywords,
                )
            except Exception as e:
                logger.error("Search failed", city=city, country=country, error=str(e))
                errors += 1
                continue

            for firm in firms:
                if added >= target:
                    break
                if self.enrichment_client._credits_exhausted:
                    break

                if not firm.get("website"):
                    skipped_no_website += 1
                    continue

                # Domain dedup (don't enrich same domain twice within a run)
                domain = (firm["website"].replace("https://", "")
                          .replace("http://", "").replace("www.", "")
                          .split("/")[0].lower())
                if domain in seen_domains:
                    skipped_dup += 1
                    continue
                seen_domains.add(domain)

                # Company dedup vs CRM
                company_lower = firm["name"].strip().lower()
                if company_lower in existing_companies:
                    skipped_dup += 1
                    continue

                # Enrich via SuperSearch
                contact = self.enrichment_client.find_contact_at_company(
                    company_name=firm["name"],
                    website=firm.get("website"),
                    city=city,
                    country=country,
                    seniority=LOGISTICS_SENIORITIES,
                    job_titles=LOGISTICS_TITLES,
                )

                if not contact or not contact.get("email"):
                    skipped_no_email += 1
                    continue

                email = contact["email"]
                if email.lower() in existing_emails:
                    skipped_dup += 1
                    continue

                lead_data = {
                    "company": firm["name"],
                    "contact_name": contact.get("full_name", ""),
                    "email": email,
                    "phone": contact.get("phone", "") or firm.get("phone", ""),
                    "website": firm.get("website", ""),
                    "industry": contact.get("industry", "logistics"),
                    "employee_count": str(contact.get("employee_count", "")),
                    "city": city,
                    "country": country,
                    "linkedin": contact.get("linkedin_url", ""),
                    "title": contact.get("title", ""),
                    "source": "google_maps + instantly_supersearch (logistics)",
                    "description": "",
                    "language": "en" if country.upper() in ("GB", "UK") else "es",
                }
                lead_data["lead_score"] = calculate_lead_score(lead_data)

                try:
                    lead_id = self.crm.add_lead(lead_data)
                    if lead_id:
                        lead_data["id"] = lead_id
                        new_leads.append(lead_data)
                        existing_emails.add(email.lower())
                        existing_companies.add(company_lower)
                        added += 1
                        logger.info("Logistics lead added",
                                    company=firm["name"],
                                    email=email,
                                    country=country,
                                    total=added)
                    else:
                        skipped_dup += 1
                except Exception as e:
                    logger.error("Error adding lead",
                                 company=firm["name"], error=str(e))
                    errors += 1

        # Personalize + queue in Instantly (unless source_only)
        queued = 0
        if not source_only and new_leads:
            queued = self._queue_in_instantly(new_leads)

        credit_summary = self.enrichment_client.get_credit_summary()
        stats = self.crm.get_stats()
        summary = {
            "added": added,
            "skipped_duplicate": skipped_dup,
            "skipped_no_website": skipped_no_website,
            "skipped_no_email": skipped_no_email,
            "errors": errors,
            "queued_in_instantly": queued,
            "total_in_sheet": stats["total_leads"],
            **credit_summary,
        }
        logger.info("Logistics sourcing complete", **summary)
        return summary

    def _queue_in_instantly(self, leads: list[dict]) -> int:
        """Push leads to the Instantly Logistics campaign.

        The email copy for Logistics lives IN Instantly, so we pass merge
        fields and let Instantly's own templates handle the body. No repo
        YAML template is applied here.
        """
        campaign_name = self.lc.get("instantly", {}).get(
            "campaign_name", "Logistics — Quote Agent"
        )

        campaigns = self.instantly.list_campaigns()
        campaign_id = None
        for c in campaigns:
            if c.get("name") == campaign_name:
                campaign_id = c.get("id")
                break

        if not campaign_id:
            logger.warning("Instantly campaign not found — skipping queue",
                           campaign_name=campaign_name)
            return 0

        # Tag language for Instantly's conditional logic (en/es)
        for lead in leads:
            first_name = ""
            if lead.get("contact_name"):
                first_name = lead["contact_name"].split()[0]
            lead["first_name"] = first_name

        result = self.instantly.add_leads_to_campaign(campaign_id, leads)
        if not result:
            logger.error("Failed to push logistics leads to Instantly")
            return 0

        for lead in leads:
            self.crm.update_lead(lead["id"], {"status": "Queued"})

        logger.info("Logistics leads queued in Instantly", count=len(leads))
        return len(leads)

    def sync_from_instantly(self) -> dict:
        """Sync engagement data back from Instantly."""
        logger.info("Syncing logistics leads from Instantly...")
        syncer = InstantlySyncer(
            instantly_api_key=self.instantly_key,
            crm=self.crm,
        )
        results = syncer.sync_all_leads()
        logger.info("Instantly sync complete",
                     crm_updated=results.get("crm_updated", 0),
                     replies_found=results.get("replies_found", 0))
        return results

    def _get_existing_companies(self) -> set[str]:
        try:
            leads = self.crm.get_leads_for_outreach(limit=1000)
            return set(
                (lead.get("company") or "").strip().lower()
                for lead in leads
                if lead.get("company")
            )
        except Exception:
            return set()


def main():
    parser = argparse.ArgumentParser(
        description="Logistics Quote Agent Pipeline (UK + Spain SMB)"
    )
    parser.add_argument("--target", type=int, help="Override daily target")
    parser.add_argument("--source-only", action="store_true",
                        help="Source + enrich only, skip Instantly queue")
    parser.add_argument("--sync-only", action="store_true",
                        help="Only sync engagement data from Instantly")
    args = parser.parse_args()

    config_dir = Path(__file__).parent.parent / "config"
    config = load_config(str(config_dir / "settings.yaml"))

    sourcer = LogisticsSourcer(config)

    if args.sync_only:
        result = sourcer.sync_from_instantly()
        print(f"\nInstantly Sync Complete!")
        print(f"  Leads synced: {result.get('crm_updated', 0)}")
        print(f"  Replies found: {result.get('replies_found', 0)}")
        return

    result = sourcer.run(target=args.target, source_only=args.source_only)

    # Log to monitor state
    try:
        from src.monitor import update_apollo_credits, update_leads_added
        update_apollo_credits(result.get("credits_used", 0))
        update_leads_added("logistics", result.get("added", 0))
    except Exception:
        pass

    print(f"\nLogistics Sourcing Complete!")
    print(f"  Added: {result['added']}")
    print(f"  Skipped (duplicate): {result['skipped_duplicate']}")
    print(f"  Skipped (no website): {result['skipped_no_website']}")
    print(f"  Skipped (no email): {result['skipped_no_email']}")
    print(f"  Errors: {result['errors']}")
    print(f"  Queued in Instantly: {result['queued_in_instantly']}")
    print(f"  SuperSearch credits used: {result.get('credits_used', 0)}")
    print(f"  Total in sheet: {result['total_in_sheet']}")


if __name__ == "__main__":
    main()
