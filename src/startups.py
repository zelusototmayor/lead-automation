"""
B2B Startups Sourcing Pipeline
================================
Standalone runner for signal-based outbound to B2B startups.
Uses SerpAPI (hiring signals via Google Jobs) → Instantly SuperSearch enrichment
→ Claude personalization → Instantly campaign.

Apollo has been dropped; the startups signal net now depends entirely on SerpAPI
Google Jobs. Kept Apollo code dormant for easy revert, but no Apollo calls are
made at runtime.

Usage:
    python src/startups.py                    # Run with defaults
    python src/startups.py --target 25        # Override target
    python src/startups.py --source-only      # Source leads only, no personalize/send
    python src/startups.py --sync-only        # Just sync from Instantly
"""

import os
import sys
import yaml
import argparse
from datetime import datetime
from pathlib import Path
import structlog

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.lead_sourcing.serpapi import SerpAPIClient, search_hiring_signals
from src.lead_sourcing.instantly_enrichment import InstantlyEnrichmentClient
from src.crm import GoogleSheetsCRM
from src.outreach import EmailPersonalizer, InstantlyClient, InstantlySyncer
from src.outreach.personalize import calculate_startup_lead_score
# Apollo dormant — not used at runtime:
# from src.lead_sourcing.apollo import ApolloClient

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
    """Load .env file into os.environ (same approach as setup scripts)."""
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


class StartupSourcer:
    """Sources B2B startups via signal-based sourcing and pushes to outreach."""

    def __init__(self, config: dict):
        self.config = config
        self.sc = config["startups"]  # startup config shorthand
        self.daily_target = self.sc["daily_target"]

        # SerpAPI (sole signal source — Apollo has been dropped)
        serpapi_key = config["api_keys"]["serpapi"]
        self.serpapi_client = SerpAPIClient(
            api_key=serpapi_key,
            budget_per_run=self.sc.get("serpapi_budget_per_run", 30),
        )

        # Instantly SuperSearch (replaces Apollo enrichment)
        supersearch_budget = self.sc.get(
            "supersearch_credit_budget",
            self.sc.get("apollo_credit_budget", 100),
        )
        self.enrichment_client = InstantlyEnrichmentClient(
            api_key=config.get("instantly", {}).get("api_key", ""),
            credit_budget=supersearch_budget,
        )

        # CRM (reuses GoogleSheetsCRM with different sheet tab)
        self.crm = GoogleSheetsCRM(
            credentials_file=config["google_sheets"]["credentials_file"],
            spreadsheet_id=config["google_sheets"]["spreadsheet_id"],
            sheet_name=self.sc.get("sheet_name", "B2B Startups"),
        )

        # Personalizer
        self.personalizer = EmailPersonalizer(
            api_key=config["api_keys"]["anthropic"],
            model=config.get("personalization", {}).get("model", "claude-sonnet-4-20250514"),
        )

        # Instantly
        self.instantly = InstantlyClient(
            api_key=config.get("instantly", {}).get("api_key", ""),
        )

        # Filters
        self.min_employees = self.sc.get("min_employees", 5)
        self.max_employees = self.sc.get("max_employees", 50)
        self.exclude_companies = self.sc.get("exclude_companies", [])

        logger.info("StartupSourcer initialized",
                     target=self.daily_target,
                     serpapi_budget=self.serpapi_client.budget_per_run,
                     supersearch_budget=supersearch_budget,
                     enrichment="instantly_supersearch")

    def _collect_signals(self) -> list[dict]:
        """Collect hiring signals from SerpAPI Google Jobs.

        Apollo signal sources (hiring_organizations, companies_with_sdrs)
        have been removed — SerpAPI is now the sole signal source.
        """
        exclude_lower = set(c.lower() for c in self.exclude_companies)
        company_signals: dict[str, dict] = {}

        hiring_queries = self.sc.get("hiring_queries", ["SDR", "BDR"])
        locations = self.sc.get("target_locations", ["United States"])

        serpapi_hiring = search_hiring_signals(
            api_key="",
            queries=hiring_queries,
            locations=locations,
            exclude_companies=self.exclude_companies,
            client=self.serpapi_client,
        )

        for s in serpapi_hiring:
            name = s["company_name"].strip()
            key = name.lower()
            if any(exc in key for exc in exclude_lower):
                continue
            if key not in company_signals:
                s["multi_signal"] = False
                s["source_count"] = 1
                s["sources"] = [s["signal_type"]]
                company_signals[key] = s

        signals = list(company_signals.values())

        logger.info("Signals collected",
                     serpapi_hiring=len(serpapi_hiring),
                     total_deduped=len(signals),
                     serpapi_searches_used=self.serpapi_client.searches_used)
        return signals

    def _is_b2b_saas(self, industry: str, keywords: list[str] = None, description: str = "") -> bool:
        """Check if a company is B2B SaaS based on industry, keywords, and description.

        Returns True if:
        - Industry matches whitelist, OR
        - Any B2B keyword found in company keywords/description (even if industry is ambiguous)
        Returns False if:
        - Industry matches blacklist AND no B2B keywords found
        """
        industry_lower = (industry or "").lower()
        keywords_lower = [k.lower() for k in (keywords or [])]
        desc_lower = (description or "").lower()
        all_text = " ".join(keywords_lower) + " " + desc_lower

        b2b_kw = self.sc.get("b2b_keywords", ["saas", "b2b", "software", "platform", "api", "cloud", "enterprise", "automation"])
        whitelist = self.sc.get("b2b_industries", [])
        blacklist = self.sc.get("exclude_industries", [])

        # Check keyword boost first — strongest signal
        has_b2b_keyword = any(kw in all_text for kw in b2b_kw)

        # Industry whitelist check
        on_whitelist = any(ind.lower() in industry_lower for ind in whitelist) if industry_lower else False

        # Industry blacklist check
        on_blacklist = any(ind.lower() in industry_lower for ind in blacklist) if industry_lower else False

        if on_blacklist and not has_b2b_keyword:
            return False
        if on_whitelist or has_b2b_keyword:
            return True
        # Ambiguous industry and no keywords — reject to keep quality high
        return False

    def _enrich_and_filter(self, signal: dict) -> dict | None:
        """Enrich a signal company via Instantly SuperSearch and apply B2B filter.

        Apollo has been dropped. SuperSearch returns industry + employee_count
        inline, so B2B + size filters run post-enrichment.
        """
        company_name = signal["company_name"]

        # Enrich directly via SuperSearch — single call for email + contact
        contact = self.enrichment_client.find_contact_at_company(
            company_name=company_name,
            website=signal.get("domain") or None,
            city=None,
            country=signal.get("country") or "US",
            seniority=["owner", "founder", "c_suite"],
        )

        if not contact or not contact.get("email"):
            logger.debug("No email found via SuperSearch", company=company_name)
            return None

        # Post-enrichment B2B filter (SuperSearch returns industry sometimes)
        industry = contact.get("industry", "")
        if industry and not self._is_b2b_saas(industry=industry, keywords=[], description=""):
            logger.debug("Filtered: not B2B SaaS", company=company_name, industry=industry)
            return None

        # Employee count filter (SuperSearch may return it)
        emp_count = contact.get("employee_count") or 0
        if isinstance(emp_count, str):
            try:
                emp_count = int(str(emp_count).replace(",", "").split("-")[0])
            except (ValueError, TypeError):
                emp_count = 0

        if emp_count and (emp_count < self.min_employees or emp_count > self.max_employees):
            logger.debug("Filtered by employee count", company=company_name, employees=emp_count)
            return None

        return {
            "company": company_name,
            "contact_name": contact.get("full_name", ""),
            "email": contact["email"],
            "phone": contact.get("phone", ""),
            "website": contact.get("company_domain", "") or signal.get("domain", ""),
            "industry": industry,
            "employee_count": emp_count,
            "city": contact.get("city", "") or signal.get("location", ""),
            "country": contact.get("country", "") or signal.get("country", ""),
            "linkedin": contact.get("linkedin_url", ""),
            "title": contact.get("title", ""),
            "source": f"{signal['signal_type']} + instantly_supersearch",
            "description": "",
            "technologies": [],
            "keywords": [],
            "signal_type": signal["signal_type"],
            "signal_detail": signal.get("signal_detail", ""),
            "multi_signal": signal.get("multi_signal", False),
            "sources": signal.get("sources", [signal["signal_type"]]),
        }

    def run(self, target: int = None, source_only: bool = False) -> dict:
        """Run the startup sourcing pipeline.

        Args:
            target: Override daily target
            source_only: If True, only source leads — skip personalization and Instantly

        Returns:
            Summary dict
        """
        target = target or self.daily_target
        logger.info("Starting startup sourcing", target=target)

        # Get existing emails for dedup
        existing_emails = self.crm.get_all_emails()
        logger.info("Existing leads in CRM", count=len(existing_emails))

        # Step 1: Collect signals
        signals = self._collect_signals()

        # Step 2: Enrich, filter, and add to CRM
        added = 0
        skipped_dup = 0
        skipped_filtered = 0
        errors = 0
        new_leads = []

        for signal in signals:
            if added >= target:
                break

            try:
                # Dedup check (company name)
                company_lower = signal["company_name"].strip().lower()

                # Enrich and filter
                lead = self._enrich_and_filter(signal)
                if not lead:
                    skipped_filtered += 1
                    continue

                # Dedup check (email)
                if lead["email"].lower() in existing_emails:
                    skipped_dup += 1
                    continue

                # Calculate lead score
                lead["lead_score"] = calculate_startup_lead_score(lead)

                # Add to CRM
                lead_id = self.crm.add_lead(lead)
                if lead_id:
                    lead["id"] = lead_id
                    new_leads.append(lead)
                    existing_emails.add(lead["email"].lower())
                    added += 1
                    logger.info("Startup lead added",
                                company=lead["company"],
                                email=lead["email"],
                                signal=lead["signal_type"],
                                score=lead["lead_score"],
                                total=added)
                else:
                    skipped_dup += 1

            except Exception as e:
                logger.error("Error processing signal",
                             company=signal.get("company_name"), error=str(e))
                errors += 1

        # Step 3: Personalize and push to Instantly (unless source_only)
        queued = 0
        if not source_only and new_leads:
            queued = self._personalize_and_queue(new_leads)

        stats = self.crm.get_stats()
        credit_summary = self.enrichment_client.get_credit_summary()
        summary = {
            "signals_collected": len(signals),
            "added": added,
            "skipped_duplicate": skipped_dup,
            "skipped_filtered": skipped_filtered,
            "errors": errors,
            "queued_in_instantly": queued,
            "serpapi_searches_used": self.serpapi_client.searches_used,
            "total_in_sheet": stats["total_leads"],
            **credit_summary,
        }

        logger.info("Startup sourcing complete", **summary)
        return summary

    def _personalize_and_queue(self, leads: list[dict]) -> int:
        """Personalize leads with Claude and push to Instantly campaign."""
        sender_info = {
            "bio": self.sc.get("personalization", {}).get("sender_bio", ""),
            "value_proposition": self.sc.get("personalization", {}).get("value_proposition", ""),
        }

        campaign_name = self.sc.get("instantly", {}).get("campaign_name", "B2B Startups Outbound")

        # Find campaign
        campaigns = self.instantly.list_campaigns()
        campaign_id = None
        for c in campaigns:
            if c.get("name") == campaign_name:
                campaign_id = c.get("id")
                break

        if not campaign_id:
            logger.warning("Instantly campaign not found — skipping queue", campaign_name=campaign_name)
            return 0

        personalized_leads = []
        for lead in leads:
            try:
                # Build signal-aware prompt context
                lead_for_personalization = dict(lead)
                signal_type = lead.get("signal_type", "")
                if signal_type in ("apollo_hiring", "hiring_signal"):
                    lead_for_personalization["signal_context"] = (
                        f"They are currently hiring: {lead.get('signal_detail', 'SDR/BDR roles')}. "
                        "This means they're investing in outbound sales."
                    )
                elif signal_type == "apollo_has_sdrs":
                    lead_for_personalization["signal_context"] = (
                        "They already have an outbound sales team in place. "
                        "This means they value outbound as a channel."
                    )
                if lead.get("multi_signal"):
                    lead_for_personalization["signal_context"] = (
                        lead_for_personalization.get("signal_context", "") +
                        " Multiple signals confirm they are actively investing in sales growth."
                    )

                personalized = self.personalizer.personalize_email(
                    lead=lead_for_personalization,
                    template="",  # We use Instantly templates
                    sender_info=sender_info,
                )

                if not personalized:
                    logger.error("Personalization returned None — skipping",
                                 company=lead.get("company"))
                    continue

                lead.update({
                    "personalized_opener": personalized.get("personalized_opener", ""),
                    "specific_pain_point": personalized.get("specific_pain_point", ""),
                    "industry_specific_insight": personalized.get("industry_specific_insight", ""),
                    "suggested_subject": personalized.get("suggested_subject", ""),
                })

                # Signal hook — a one-liner referencing WHY we're reaching out
                first_name = (lead.get("contact_name") or "").split()[0] if lead.get("contact_name") else ""
                if signal_type in ("apollo_hiring", "hiring_signal"):
                    lead["signal_hook"] = (
                        f"I noticed {lead.get('company', 'your team')} is hiring sales reps — "
                        "what if you could get the same pipeline output without the headcount?"
                    )
                elif signal_type == "apollo_has_sdrs":
                    lead["signal_hook"] = (
                        f"I see {lead.get('company', 'your team')} already runs outbound — "
                        "curious if your SDRs are spending more time on research or actually selling?"
                    )
                else:
                    lead["signal_hook"] = ""

                personalized_leads.append(lead)

            except Exception as e:
                logger.error("Personalization failed", company=lead.get("company"), error=str(e))

        # Push to Instantly
        if personalized_leads:
            result = self.instantly.add_leads_to_campaign(campaign_id, personalized_leads)
            if result:
                for lead in personalized_leads:
                    self.crm.update_lead(lead["id"], {"status": "Queued"})
                logger.info("Leads queued in Instantly", count=len(personalized_leads))
            else:
                logger.error("Failed to push leads to Instantly")
                return 0

        return len(personalized_leads)

    def sync_from_instantly(self) -> dict:
        """Sync engagement data from Instantly back to CRM."""
        logger.info("Syncing startup leads from Instantly...")
        syncer = InstantlySyncer(
            instantly_api_key=self.config.get("instantly", {}).get("api_key", ""),
            crm=self.crm,
        )
        results = syncer.sync_all_leads()
        logger.info("Instantly sync complete",
                     crm_updated=results.get("crm_updated", 0),
                     replies_found=results.get("replies_found", 0))
        return results


def main():
    parser = argparse.ArgumentParser(description="B2B Startups Lead Sourcing Pipeline")
    parser.add_argument("--target", type=int, help="Override daily target")
    parser.add_argument("--source-only", action="store_true",
                        help="Source leads only, skip personalization and Instantly")
    parser.add_argument("--sync-only", action="store_true",
                        help="Only sync engagement data from Instantly")
    args = parser.parse_args()

    config_dir = Path(__file__).parent.parent / "config"
    config = load_config(str(config_dir / "settings.yaml"))

    sourcer = StartupSourcer(config)

    if args.sync_only:
        result = sourcer.sync_from_instantly()
        print(f"\nInstantly Sync Complete!")
        print(f"  Leads synced: {result.get('crm_updated', 0)}")
        print(f"  Replies found: {result.get('replies_found', 0)}")
        return

    result = sourcer.run(target=args.target, source_only=args.source_only)

    # Log credits to monitor state
    from src.monitor import update_apollo_credits, update_leads_added
    update_apollo_credits(result.get("credits_used", 0))
    update_leads_added("startups", result.get("added", 0))

    print(f"\nB2B Startups Sourcing Complete!")
    print(f"  Signals collected: {result['signals_collected']}")
    print(f"  Leads added: {result['added']}")
    print(f"  Skipped (duplicate): {result['skipped_duplicate']}")
    print(f"  Skipped (filtered): {result['skipped_filtered']}")
    print(f"  Errors: {result['errors']}")
    print(f"  Queued in Instantly: {result['queued_in_instantly']}")
    print(f"  SerpAPI searches used: {result['serpapi_searches_used']}")
    print(f"  SuperSearch credits used: {result.get('credits_used', 0)}")
    print(f"  Total in sheet: {result['total_in_sheet']}")


if __name__ == "__main__":
    main()
