"""
Lead Automation - Main Orchestrator
====================================
Daily workflow that sources leads, enriches them, and adds to outreach campaigns.
"""

import os
import sys
import yaml
import random
from datetime import datetime
from pathlib import Path
import structlog

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.lead_sourcing import search_agencies, enrich_lead
from src.crm import GoogleSheetsCRM
from src.outreach import EmailPersonalizer, calculate_lead_score, InstantlyClient, sync_from_instantly

# Configure logging
structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer()
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
)

logger = structlog.get_logger()


def load_config(config_path: str = "config/settings.yaml") -> dict:
    """Load configuration from YAML file."""
    with open(config_path, 'r') as f:
        config = yaml.safe_load(f)

    # Replace environment variable placeholders
    def replace_env_vars(obj):
        if isinstance(obj, str) and obj.startswith("${") and obj.endswith("}"):
            env_var = obj[2:-1]
            return os.environ.get(env_var, "")
        elif isinstance(obj, dict):
            return {k: replace_env_vars(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [replace_env_vars(item) for item in obj]
        return obj

    return replace_env_vars(config)


def load_email_templates(template_path: str = "config/email_templates.yaml") -> dict:
    """Load email templates from YAML file."""
    with open(template_path, 'r') as f:
        return yaml.safe_load(f)


class LeadAutomation:
    """Main automation orchestrator."""

    def __init__(self, config: dict, templates: dict):
        """
        Initialize the automation.

        Args:
            config: Configuration dictionary
            templates: Email templates dictionary
        """
        self.config = config
        self.templates = templates

        # Initialize components
        self.crm = GoogleSheetsCRM(
            credentials_file=config["google_sheets"]["credentials_file"],
            spreadsheet_id=config["google_sheets"]["spreadsheet_id"],
            sheet_name=config["google_sheets"]["sheet_name"]
        )

        self.personalizer = EmailPersonalizer(
            api_key=config["api_keys"]["anthropic"],
            model=config["personalization"]["model"]
        )

        self.instantly = InstantlyClient(
            api_key=config.get("instantly", {}).get("api_key", "")
        )

        # API keys
        self.google_maps_key = config["api_keys"]["google_maps"]
        self.apollo_key = config["api_keys"]["apollo"]

        logger.info("Lead automation initialized")

    def run_daily_sourcing(self, target_leads: int = None) -> list[dict]:
        """
        Run the daily lead sourcing workflow.

        Args:
            target_leads: Number of leads to find (defaults to config value)

        Returns:
            List of new leads added to CRM
        """
        target_leads = target_leads or self.config["lead_sourcing"]["daily_target"]
        cities = self.config["lead_sourcing"]["target_cities"]
        search_queries = self.config["lead_sourcing"]["search_queries"]
        exclude_keywords = self.config["lead_sourcing"]["exclude_keywords"]

        logger.info("Starting daily lead sourcing", target=target_leads)

        # Get existing emails to avoid duplicates
        existing_emails = self.crm.get_all_emails()
        logger.info("Existing leads in CRM", count=len(existing_emails))

        new_leads = []
        leads_needed = target_leads

        # Shuffle cities to get variety
        random.shuffle(cities)

        for city_info in cities:
            if leads_needed <= 0:
                break

            city = city_info["name"]
            country = city_info["country"]

            logger.info("Searching city", city=city, country=country)

            # Search for agencies
            agencies = search_agencies(
                api_key=self.google_maps_key,
                city=city,
                country=country,
                search_queries=random.sample(search_queries, min(3, len(search_queries))),
                max_per_query=5,
                exclude_keywords=exclude_keywords
            )

            for agency in agencies:
                if leads_needed <= 0:
                    break

                # Skip if no website
                if not agency.get("website"):
                    continue

                # Enrich with Apollo
                enriched = enrich_lead(
                    api_key=self.apollo_key,
                    company_name=agency["name"],
                    website=agency.get("website"),
                    city=city
                )

                # Get primary contact
                primary_contact = enriched.get("primary_contact", {})
                email = primary_contact.get("email", "")

                # Skip if no email or duplicate
                if not email or email.lower() in existing_emails:
                    continue

                # Calculate lead score
                lead_data = {
                    "company": agency["name"],
                    "contact_name": primary_contact.get("full_name", ""),
                    "email": email,
                    "phone": agency.get("phone", ""),
                    "website": agency.get("website", ""),
                    "industry": enriched.get("industry", ""),
                    "employee_count": enriched.get("employee_count", ""),
                    "city": city,
                    "country": country,
                    "linkedin": primary_contact.get("linkedin_url", ""),
                    "title": primary_contact.get("title", ""),
                    "source": "google_maps + apollo",
                    "description": enriched.get("description", ""),
                    "technologies": enriched.get("technologies", []),
                    "keywords": enriched.get("keywords", [])
                }

                lead_data["lead_score"] = calculate_lead_score(lead_data)

                # Add to CRM
                lead_id = self.crm.add_lead(lead_data)
                if lead_id:
                    lead_data["id"] = lead_id
                    new_leads.append(lead_data)
                    existing_emails.add(email.lower())
                    leads_needed -= 1

                    logger.info(
                        "Lead added",
                        company=agency["name"],
                        email=email,
                        score=lead_data["lead_score"]
                    )

        logger.info("Daily sourcing complete", new_leads=len(new_leads))
        return new_leads

    def personalize_and_queue_leads(self, leads: list[dict] = None) -> int:
        """
        Personalize emails for leads and add them to Instantly campaign.

        Args:
            leads: List of leads to process (defaults to new leads in CRM)

        Returns:
            Number of leads queued
        """
        if leads is None:
            leads = self.crm.get_leads_for_outreach(limit=10)

        if not leads:
            logger.info("No leads to process")
            return 0

        logger.info("Personalizing leads", count=len(leads))

        # Get email template
        sequence = self.templates["sequences"]["default"]["emails"][0]
        template = sequence["body_template"]

        sender_info = {
            "bio": self.config["personalization"]["sender_bio"],
            "value_proposition": self.config["personalization"]["value_proposition"]
        }

        # Personalize each lead
        personalized_leads = []
        for lead in leads:
            try:
                # Generate personalized content
                personalized = self.personalizer.personalize_email(
                    lead=lead,
                    template=template,
                    sender_info=sender_info
                )

                # Add personalization to lead data
                lead.update({
                    "personalized_opener": personalized.get("personalized_opener", ""),
                    "specific_pain_point": personalized.get("specific_pain_point", ""),
                    "industry_specific_insight": personalized.get("industry_specific_insight", ""),
                    "first_name": lead.get("contact_name", "").split()[0] if lead.get("contact_name") else ""
                })

                personalized_leads.append(lead)

                logger.info("Lead personalized", company=lead.get("company"))

            except Exception as e:
                logger.error("Failed to personalize lead", company=lead.get("company"), error=str(e))

        # Add to Instantly campaign
        if personalized_leads:
            campaign_name = self.config.get("instantly", {}).get("campaign_name", "Agency Outreach")

            # Find or create campaign
            campaigns = self.instantly.list_campaigns()
            campaign_id = None
            for campaign in campaigns:
                if campaign.get("name") == campaign_name:
                    campaign_id = campaign.get("id")
                    break

            if campaign_id:
                result = self.instantly.add_leads_to_campaign(campaign_id, personalized_leads)
                if result:
                    # Mark leads as queued in CRM
                    for lead in personalized_leads:
                        self.crm.update_lead(lead["id"], {"status": "Queued"})

                    logger.info("Leads added to Instantly", count=len(personalized_leads))
            else:
                logger.warning("Campaign not found in Instantly", campaign_name=campaign_name)

        return len(personalized_leads)

    def sync_from_instantly(self):
        """Sync lead data (opens, clicks, replies) from Instantly to CRM."""
        logger.info("Syncing data from Instantly...")

        try:
            results = sync_from_instantly(
                instantly_api_key=self.config.get("instantly", {}).get("api_key", ""),
                credentials_file=self.config["google_sheets"]["credentials_file"],
                spreadsheet_id=self.config["google_sheets"]["spreadsheet_id"]
            )
            logger.info(
                "Instantly sync complete",
                leads_synced=results.get("crm_updated", 0),
                replies_found=results.get("replies_found", 0)
            )
            return results
        except Exception as e:
            logger.error("Instantly sync failed", error=str(e))
            return {"error": str(e)}

    def run_full_workflow(self):
        """Run the complete daily workflow."""
        logger.info("=" * 50)
        logger.info("Starting full daily workflow", timestamp=datetime.now().isoformat())
        logger.info("=" * 50)

        # Step 1: Sync existing leads from Instantly (get opens/replies)
        sync_results = self.sync_from_instantly()
        logger.info(f"Step 1 complete: Synced {sync_results.get('crm_updated', 0)} leads from Instantly")

        # Step 2: Source new leads
        new_leads = self.run_daily_sourcing()
        logger.info(f"Step 2 complete: Found {len(new_leads)} new leads")

        # Step 3: Personalize and queue for outreach
        queued = 0
        if new_leads:
            queued = self.personalize_and_queue_leads(new_leads)
            logger.info(f"Step 3 complete: Queued {queued} leads for outreach")

        # Step 4: Get and log stats
        stats = self.crm.get_stats()
        logger.info("CRM Stats", **stats)

        logger.info("=" * 50)
        logger.info("Daily workflow complete")
        logger.info("=" * 50)

        return {
            "synced": sync_results.get("crm_updated", 0),
            "replies_found": sync_results.get("replies_found", 0),
            "new_leads": len(new_leads),
            "queued": queued,
            "stats": stats
        }


def main():
    """Main entry point."""
    # Determine config path
    config_dir = Path(__file__).parent.parent / "config"

    # Load configuration
    config = load_config(str(config_dir / "settings.yaml"))
    templates = load_email_templates(str(config_dir / "email_templates.yaml"))

    # Run automation
    automation = LeadAutomation(config, templates)
    result = automation.run_full_workflow()

    print(f"\n✅ Daily workflow complete!")
    print(f"   Synced from Instantly: {result.get('synced', 0)} leads")
    print(f"   Replies found: {result.get('replies_found', 0)}")
    print(f"   New leads found: {result['new_leads']}")
    print(f"   Leads queued: {result['queued']}")
    print(f"   Total in CRM: {result['stats']['total_leads']}")


if __name__ == "__main__":
    main()
