"""
Local Services Sourcing Pipeline
==================================
Standalone runner for phone-first outreach to local-service businesses.
Separate from the agency email pipeline — outputs a contact list with
Location, Website, Phone, POC.

Usage:
    python src/local_services.py                  # Run with defaults from config
    python src/local_services.py --target 10      # Override daily target
"""

import os
import sys
import yaml
import random
import argparse
from datetime import datetime
from pathlib import Path
import structlog

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.lead_sourcing import search_agencies
from src.lead_sourcing.apollo import ApolloClient
from src.crm import LocalServicesCRM

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


def load_config(config_path: str = "config/settings.yaml") -> dict:
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


class LocalServicesSourcer:
    """Sources local-service businesses for phone outreach."""

    def __init__(self, config: dict):
        self.config = config
        ls_config = config["local_services"]
        self.daily_target = ls_config["daily_target"]
        self.max_per_query = ls_config.get("max_per_query", 5)
        self.metros = ls_config["target_metros"]
        self.verticals = ls_config["verticals"]

        self.google_maps_key = config["api_keys"]["google_maps"]
        self.apollo_key = config["api_keys"]["apollo"]
        self.apollo_client = ApolloClient(self.apollo_key)

        self.crm = LocalServicesCRM(
            credentials_file=config["google_sheets"]["credentials_file"],
            spreadsheet_id=config["google_sheets"]["spreadsheet_id"],
            sheet_name=ls_config.get("sheet_name", "Local Services"),
        )

        logger.info("LocalServicesSourcer initialized", metros=len(self.metros), verticals=len(self.verticals))

    def run(self, target: int = None) -> dict:
        target = target or self.daily_target
        logger.info("Starting local services sourcing", target=target)

        existing = self.crm.get_all_companies()
        logger.info("Existing leads", count=len(existing))

        added = 0
        skipped_dup = 0
        skipped_no_poc = 0
        errors = 0

        # Build metro x vertical combos and shuffle for variety
        combos = [
            (metro, vertical_name, vertical_cfg)
            for metro in self.metros
            for vertical_name, vertical_cfg in self.verticals.items()
        ]
        random.shuffle(combos)

        for metro, vertical_name, vertical_cfg in combos:
            if added >= target:
                break

            city = metro["name"]
            state = metro["state"]
            queries = vertical_cfg["queries"]
            exclude = vertical_cfg.get("exclude", [])

            # Pick a subset of queries per combo to stay within budget
            sample_size = min(2, len(queries))
            sampled_queries = random.sample(queries, sample_size)

            logger.info("Searching", city=city, vertical=vertical_name, queries=sampled_queries)

            try:
                agencies = search_agencies(
                    api_key=self.google_maps_key,
                    city=city,
                    country="US",
                    search_queries=sampled_queries,
                    max_per_query=self.max_per_query,
                    exclude_keywords=exclude,
                )
            except Exception as e:
                logger.error("Search failed", city=city, vertical=vertical_name, error=str(e))
                errors += 1
                continue

            for agency in agencies:
                if added >= target:
                    break

                if not agency.get("website"):
                    continue

                # Quick dedup before enrichment
                company_key = (agency["name"].strip().lower(), city.lower())
                if company_key in existing:
                    skipped_dup += 1
                    continue

                # Find POC using free Apollo search (no credits)
                poc_name = ""
                poc_title = ""
                try:
                    contacts = self.apollo_client.find_contacts_free(
                        company_domain=agency.get("website"),
                        company_name=agency["name"],
                        limit=1,
                    )
                    if contacts:
                        poc_name = contacts[0].get("full_name", "")
                        poc_title = contacts[0].get("title", "")
                except Exception as e:
                    logger.error("POC lookup failed", company=agency["name"], error=str(e))
                    errors += 1

                if not poc_name and not agency.get("phone"):
                    skipped_no_poc += 1
                    continue

                lead_data = {
                    "company": agency["name"],
                    "poc_name": poc_name,
                    "poc_title": poc_title,
                    "phone": agency.get("phone", ""),
                    "website": agency.get("website", ""),
                    "city": city,
                    "state": state,
                    "vertical": vertical_name,
                }

                lead_id = self.crm.add_lead(lead_data)
                if lead_id:
                    added += 1
                    existing.add(company_key)
                    logger.info("Lead added", company=agency["name"], city=city, vertical=vertical_name, total=added)
                else:
                    skipped_dup += 1

        stats = self.crm.get_stats()
        summary = {
            "added": added,
            "skipped_duplicate": skipped_dup,
            "skipped_no_poc": skipped_no_poc,
            "errors": errors,
            "total_in_sheet": stats["total_leads"],
            "by_vertical": stats["by_vertical"],
            "by_metro": stats["by_metro"],
        }

        logger.info("Local services sourcing complete", **summary)
        return summary


def main():
    parser = argparse.ArgumentParser(description="Local Services Lead Sourcing")
    parser.add_argument("--target", type=int, help="Override daily target")
    args = parser.parse_args()

    config_dir = Path(__file__).parent.parent / "config"
    config = load_config(str(config_dir / "settings.yaml"))

    sourcer = LocalServicesSourcer(config)
    result = sourcer.run(target=args.target)

    print(f"\nLocal Services Sourcing Complete!")
    print(f"  Added: {result['added']}")
    print(f"  Skipped (duplicate): {result['skipped_duplicate']}")
    print(f"  Skipped (no POC): {result['skipped_no_poc']}")
    print(f"  Errors: {result['errors']}")
    print(f"  Total in sheet: {result['total_in_sheet']}")
    if result["by_vertical"]:
        print(f"  By vertical: {result['by_vertical']}")
    if result["by_metro"]:
        print(f"  By metro: {result['by_metro']}")


if __name__ == "__main__":
    main()
