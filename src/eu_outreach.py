"""
EU B2B Outreach Pipeline (PT/UK/ES)
=====================================
Signal-based sourcing: finds B2B companies hiring SDR/BDR roles
in Portugal, United Kingdom, and Spain via LinkedIn (Apify),
then enriches with Apollo for email-ready leads → Instantly.

Flow: Apify LinkedIn signals → B2B filter (description) → Apollo org lookup (FREE)
      → Apollo contact enrichment (1 credit) → Claude personalization → Instantly

Usage:
    python src/eu_outreach.py                     # Run with defaults
    python src/eu_outreach.py --target 30         # Override target
    python src/eu_outreach.py --source-only       # Source leads only, no personalize/send
    python src/eu_outreach.py --sync-only         # Just sync from Instantly
"""

import os
import sys
import yaml
import argparse
from pathlib import Path
import structlog

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.lead_sourcing.apify import ApifyClient, search_linkedin_hiring_signals
from src.lead_sourcing.apollo import ApolloClient
from src.crm import GoogleSheetsCRM
from src.outreach import EmailPersonalizer, InstantlyClient, InstantlySyncer

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


class EUOutreachSourcer:
    """Sources B2B companies hiring SDR/BDR in PT/UK/ES via LinkedIn + Apollo.

    Apify collects hiring signals from LinkedIn, then Apollo enriches with
    org data (FREE) and contact emails (1 credit each). Leads pushed to
    Instantly for automated email outreach.
    """

    def __init__(self, config: dict):
        self.config = config
        self.ec = config["eu_outreach"]
        self.daily_target = self.ec["daily_target"]

        # Apify (LinkedIn Jobs)
        self.apify_client = ApifyClient(
            api_key=config["api_keys"]["apify"],
            max_runs_per_session=self.ec.get("apify_max_runs", 20),
        )

        # Apollo (org lookup FREE, contact enrichment costs credits)
        apollo_budget = self.ec.get("apollo_credit_budget", 100)
        self.apollo_client = ApolloClient(
            config["api_keys"]["apollo"],
            credit_budget=apollo_budget,
        )

        # CRM
        self.crm = GoogleSheetsCRM(
            credentials_file=config["google_sheets"]["credentials_file"],
            spreadsheet_id=config["google_sheets"]["spreadsheet_id"],
            sheet_name=self.ec.get("sheet_name", "EU B2B Leads"),
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
        self.exclude_companies = self.ec.get("exclude_companies", [])
        self.b2b_keywords = self.ec.get("b2b_keywords", [
            "saas", "b2b", "software", "platform", "api", "cloud",
            "enterprise", "automation", "analytics", "data",
        ])
        self.blacklist_keywords = self.ec.get("blacklist_keywords", [
            "restaurant", "hotel", "hospitality", "retail store",
            "staffing agency", "recruitment agency", "nursing",
            "healthcare", "medical", "school", "university",
            "government", "non-profit", "nonprofit", "charity",
        ])
        self.exclude_industries = self.ec.get("exclude_industries", [])

        logger.info("EUOutreachSourcer initialized",
                     target=self.daily_target,
                     apollo_budget=apollo_budget,
                     locations=self.ec.get("target_locations", []))

    def _collect_signals(self) -> list[dict]:
        """Collect hiring signals from LinkedIn via Apify."""
        queries = self.ec.get("hiring_queries", [])
        locations = self.ec.get("target_locations", [])
        max_per_search = self.ec.get("max_results_per_search", 200)

        signals = search_linkedin_hiring_signals(
            client=self.apify_client,
            queries=queries,
            locations=locations,
            exclude_companies=self.exclude_companies,
            max_results_per_search=max_per_search,
        )

        logger.info("Signals collected",
                     total=len(signals),
                     apify_runs=self.apify_client.runs_used)
        return signals

    def _is_b2b_from_description(self, description: str, company_name: str) -> bool:
        """Filter B2B companies using the job description text."""
        text = (description or "").lower() + " " + (company_name or "").lower()

        if any(kw in text for kw in self.blacklist_keywords):
            return False

        if any(kw in text for kw in self.b2b_keywords):
            return True

        b2b_job_signals = [
            "outbound", "pipeline", "prospecting", "cold call",
            "cold email", "demo", "qualified leads", "iq",
            "crm", "salesforce", "hubspot", "linkedin sales navigator",
            "decision maker", "c-level", "stakeholder",
            "revenue", "arr", "mrr", "quota",
            "account executive", "closing", "sdr", "bdr",
        ]
        if any(kw in text for kw in b2b_job_signals):
            return True

        return False

    def _is_b2b_from_org(self, industry: str, keywords: list[str] = None, description: str = "") -> bool:
        """Check if company is B2B using Apollo org data (industry + keywords)."""
        industry_lower = (industry or "").lower()
        all_text = " ".join(k.lower() for k in (keywords or [])) + " " + (description or "").lower()

        # Industry blacklist
        if any(ind.lower() in industry_lower for ind in self.exclude_industries) if industry_lower else False:
            if not any(kw in all_text for kw in self.b2b_keywords):
                return False

        # B2B keyword match
        if any(kw in all_text for kw in self.b2b_keywords):
            return True

        # If we got here with a good industry, pass (EU companies often have sparse keyword data)
        if industry_lower and not any(ind.lower() in industry_lower for ind in self.exclude_industries):
            return True

        return False

    @staticmethod
    def _pick_seniority(employee_count: int) -> list[str]:
        """Pick the best seniority levels to target based on company size.

        Smaller companies: target founders/owners (they make buying decisions).
        Larger companies: target VP/Director of Sales (they own outbound budget).
        """
        if not employee_count or employee_count <= 15:
            return ["owner", "founder", "c_suite"]
        elif employee_count <= 50:
            return ["c_suite", "founder", "vp"]
        else:
            return ["vp", "director", "c_suite"]

    def _enrich_and_filter(self, signal: dict) -> dict | None:
        """Enrich a signal with Apollo and apply B2B filter.

        1. B2B filter from job description (already passed before calling this)
        2. Apollo org lookup (FREE) — get industry, domain, employee count
        3. B2B filter from Apollo data (double-check)
        4. Apollo contact enrichment (1 credit) — get decision-maker email
        """
        company_name = signal["company_name"]

        # Apollo org lookup (FREE)
        org = self.apollo_client.search_organizations(company_name=company_name)
        if not org:
            self.apollo_client._orgs_skipped_no_data += 1
            logger.debug("No Apollo org data — skipping", company=company_name)
            return None

        # Double-check B2B from Apollo data
        if not self._is_b2b_from_org(
            industry=org.get("industry", ""),
            keywords=org.get("keywords", []),
            description=org.get("description", ""),
        ):
            logger.debug("Filtered: not B2B (Apollo data)", company=company_name,
                         industry=org.get("industry"))
            return None

        # Pick seniority based on company size
        emp_count = org.get("employee_count") or 0
        if isinstance(emp_count, str):
            try:
                emp_count = int(str(emp_count).replace(",", "").split("-")[0])
            except (ValueError, TypeError):
                emp_count = 0
        seniority = self._pick_seniority(emp_count)

        # Find contact with email (costs 1 credit)
        contacts = self.apollo_client.find_contacts(
            company_domain=org.get("domain"),
            company_name=company_name,
            seniority=seniority,
            limit=1,
        )

        if not contacts or not contacts[0].get("email"):
            logger.debug("No email found", company=company_name)
            return None

        contact = contacts[0]

        # Build notes with LinkedIn job posting info
        job_url = signal.get("job_url", "")
        job_title = signal.get("job_title", "")
        notes_parts = []
        if job_url:
            notes_parts.append(job_url)
        if job_title:
            notes_parts.append(f"Role: {job_title}")

        return {
            "company": company_name,
            "contact_name": contact.get("full_name", ""),
            "email": contact["email"],
            "phone": contact.get("phone", ""),
            "website": org.get("domain", ""),
            "industry": org.get("industry", ""),
            "employee_count": emp_count,
            "city": org.get("city", signal.get("city", "")),
            "country": org.get("country", signal.get("country", "")),
            "linkedin": contact.get("linkedin_url", ""),
            "title": contact.get("title", ""),
            "source": f"linkedin_apify | {signal.get('signal_detail', '')}",
            "description": org.get("description", ""),
            "keywords": org.get("keywords", []),
            "notes": " | ".join(notes_parts),
            "signal_detail": signal.get("signal_detail", ""),
            "lead_score": 0,
            "status": "New",
        }

    def run(self, target: int = None, source_only: bool = False) -> dict:
        """Run the EU outreach sourcing pipeline."""
        target = target or self.daily_target
        logger.info("Starting EU outreach sourcing", target=target)

        # Get existing emails for dedup
        existing_emails = self.crm.get_all_emails()
        existing_companies = self._get_existing_companies()
        logger.info("Existing leads in CRM",
                     emails=len(existing_emails),
                     companies=len(existing_companies))

        # Step 1: Collect hiring signals from LinkedIn
        signals = self._collect_signals()

        # Step 2: Filter B2B (description), enrich with Apollo, add to CRM
        added = 0
        skipped_dup = 0
        skipped_filtered = 0
        skipped_no_data = 0
        errors = 0
        new_leads = []

        for signal in signals:
            if added >= target:
                break

            # Budget check — stop if Apollo credits exhausted
            if self.apollo_client._credits_exhausted:
                logger.warning("Apollo credits exhausted — stopping enrichment")
                break

            company_lower = signal["company_name"].strip().lower()

            # Dedup by company name
            if company_lower in existing_companies:
                skipped_dup += 1
                continue

            # First filter: B2B from job description (free, fast)
            if not self._is_b2b_from_description(
                signal.get("description_text", ""),
                signal["company_name"],
            ):
                skipped_filtered += 1
                continue

            try:
                # Enrich with Apollo (org lookup FREE, contact 1 credit)
                lead = self._enrich_and_filter(signal)
                if not lead:
                    skipped_no_data += 1
                    continue

                # Dedup by email
                if lead["email"].lower() in existing_emails:
                    skipped_dup += 1
                    continue

                # Add to CRM
                lead_id = self.crm.add_lead(lead)
                if lead_id:
                    lead["id"] = lead_id
                    new_leads.append(lead)
                    existing_emails.add(lead["email"].lower())
                    existing_companies.add(company_lower)
                    added += 1
                    logger.info("EU lead added",
                                company=lead["company"],
                                email=lead["email"],
                                country=lead["country"],
                                source=lead["source"],
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
        credit_summary = self.apollo_client.get_credit_summary()
        summary = {
            "signals_collected": len(signals),
            "added": added,
            "skipped_duplicate": skipped_dup,
            "skipped_filtered": skipped_filtered,
            "skipped_no_data": skipped_no_data,
            "errors": errors,
            "queued_in_instantly": queued,
            "apify_runs_used": self.apify_client.runs_used,
            "total_in_sheet": stats["total_leads"],
            **credit_summary,
        }

        logger.info("EU outreach sourcing complete", **summary)
        return summary

    def _personalize_and_queue(self, leads: list[dict]) -> int:
        """Personalize leads with Claude and push to Instantly campaign."""
        sender_info = {
            "bio": self.ec.get("personalization", {}).get("sender_bio", ""),
            "value_proposition": self.ec.get("personalization", {}).get("value_proposition", ""),
        }

        campaign_name = self.ec.get("instantly", {}).get("campaign_name", "EU B2B Outbound")

        # Find campaign
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

        personalized_leads = []
        for lead in leads:
            try:
                lead_for_personalization = dict(lead)
                lead_for_personalization["signal_context"] = (
                    f"They are currently hiring SDR/BDR roles in Europe "
                    f"({lead.get('signal_detail', '')}). "
                    "This means they're investing in outbound sales."
                )

                personalized = self.personalizer.personalize_email(
                    lead=lead_for_personalization,
                    template="",
                    sender_info=sender_info,
                )

                lead.update({
                    "personalized_opener": personalized.get("personalized_opener", ""),
                    "specific_pain_point": personalized.get("specific_pain_point", ""),
                    "industry_specific_insight": personalized.get("industry_specific_insight", ""),
                    "suggested_subject": personalized.get("suggested_subject", ""),
                })

                # Signal hook referencing the hiring signal
                lead["signal_hook"] = (
                    f"I noticed {lead.get('company', 'your team')} is hiring sales reps — "
                    "what if you could get the same pipeline output without the headcount?"
                )

                personalized_leads.append(lead)

            except Exception as e:
                logger.error("Personalization failed",
                             company=lead.get("company"), error=str(e))

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
        logger.info("Syncing EU leads from Instantly...")
        syncer = InstantlySyncer(
            instantly_api_key=self.config.get("instantly", {}).get("api_key", ""),
            crm=self.crm,
        )
        results = syncer.sync_all_leads()
        logger.info("Instantly sync complete",
                     crm_updated=results.get("crm_updated", 0),
                     replies_found=results.get("replies_found", 0))
        return results

    def _get_existing_companies(self) -> set[str]:
        """Get existing company names from CRM for dedup."""
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
        description="EU B2B Outreach Pipeline (PT/UK/ES)"
    )
    parser.add_argument("--target", type=int, help="Override daily target")
    parser.add_argument("--source-only", action="store_true",
                        help="Source leads only, skip personalization and Instantly")
    parser.add_argument("--sync-only", action="store_true",
                        help="Only sync engagement data from Instantly")
    args = parser.parse_args()

    config_dir = Path(__file__).parent.parent / "config"
    config = load_config(str(config_dir / "settings.yaml"))

    sourcer = EUOutreachSourcer(config)

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
    update_leads_added("eu", result.get("added", 0))

    print(f"\nEU B2B Outreach Sourcing Complete!")
    print(f"  Signals collected: {result['signals_collected']}")
    print(f"  Leads added: {result['added']}")
    print(f"  Skipped (duplicate): {result['skipped_duplicate']}")
    print(f"  Skipped (filtered): {result['skipped_filtered']}")
    print(f"  Skipped (no Apollo data): {result['skipped_no_data']}")
    print(f"  Errors: {result['errors']}")
    print(f"  Queued in Instantly: {result['queued_in_instantly']}")
    print(f"  Apify runs used: {result['apify_runs_used']}")
    print(f"  Apollo credits used: {result['credits_used']}")
    print(f"  Credits per lead: {result['credits_per_lead']}")
    print(f"  Total in sheet: {result['total_in_sheet']}")


if __name__ == "__main__":
    main()
