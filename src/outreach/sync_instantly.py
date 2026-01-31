"""
Instantly CRM Sync
==================
Syncs lead data (opens, clicks, replies, status) from Instantly.ai back to Google Sheets CRM.
"""

import os
from datetime import datetime
from typing import Optional
import structlog

from .instantly_client import InstantlyClient
from ..crm.sheets import GoogleSheetsCRM

logger = structlog.get_logger()


# Instantly status codes
INSTANTLY_STATUS = {
    0: "Not Started",
    1: "Active",
    2: "Paused",
    3: "Completed",
    4: "Bounced",
    5: "Unsubscribed",
    6: "Replied",
    7: "Interested",
    8: "Not Interested",
    9: "Meeting Booked",
    10: "Closed"
}


class InstantlySyncer:
    """Syncs lead data from Instantly to Google Sheets CRM."""

    def __init__(
        self,
        instantly_api_key: str,
        crm: GoogleSheetsCRM,
        campaign_id: str = None
    ):
        """
        Initialize the syncer.

        Args:
            instantly_api_key: Instantly API key
            crm: GoogleSheetsCRM instance
            campaign_id: Optional specific campaign ID to sync
        """
        self.instantly = InstantlyClient(instantly_api_key)
        self.crm = crm
        self.campaign_id = campaign_id

    def get_campaigns(self) -> list[dict]:
        """Get all campaigns or the specific one configured."""
        if self.campaign_id:
            campaign = self.instantly.get_campaign(self.campaign_id)
            return [campaign] if campaign else []
        return self.instantly.list_campaigns()

    def sync_all_leads(self) -> dict:
        """
        Sync all lead data from Instantly to CRM.

        Returns:
            Summary of sync results
        """
        results = {
            "campaigns_checked": 0,
            "leads_checked": 0,
            "crm_updated": 0,
            "replies_found": 0,
            "not_in_crm": 0,
            "errors": []
        }

        campaigns = self.get_campaigns()
        results["campaigns_checked"] = len(campaigns)

        logger.info("Starting Instantly sync", campaigns=len(campaigns))

        for campaign in campaigns:
            campaign_id = campaign.get("id")
            campaign_name = campaign.get("name", "Unknown")

            logger.info("Syncing campaign", name=campaign_name)

            try:
                # Get all leads from campaign (API limit is ~100)
                leads = self.instantly.list_leads(campaign_id, limit=100)
                results["leads_checked"] += len(leads)

                for lead in leads:
                    email = lead.get("email")
                    if not email:
                        continue

                    # Prepare data to sync
                    sync_data = {
                        "opens": lead.get("email_open_count", 0),
                        "clicks": lead.get("email_click_count", 0),
                        "instantly_status": INSTANTLY_STATUS.get(
                            lead.get("status", 0),
                            f"Unknown ({lead.get('status')})"
                        )
                    }

                    # Check for replies
                    reply_count = lead.get("email_reply_count", 0)
                    if reply_count > 0:
                        results["replies_found"] += 1
                        sync_data["response"] = f"Replied ({reply_count} replies)"

                    # Update CRM
                    updated = self.crm.update_from_instantly(email, sync_data)

                    if updated:
                        results["crm_updated"] += 1
                    else:
                        results["not_in_crm"] += 1

            except Exception as e:
                error_msg = f"Error syncing campaign {campaign_name}: {str(e)}"
                logger.error(error_msg)
                results["errors"].append(error_msg)

        logger.info(
            "Instantly sync complete",
            leads_checked=results["leads_checked"],
            crm_updated=results["crm_updated"],
            replies_found=results["replies_found"]
        )

        return results


def sync_from_instantly(
    instantly_api_key: str = None,
    credentials_file: str = None,
    spreadsheet_id: str = None,
    campaign_id: str = None
) -> dict:
    """
    Convenience function to sync from Instantly.

    Args:
        instantly_api_key: Instantly API key (or from env)
        credentials_file: Path to Google credentials JSON
        spreadsheet_id: Google Sheets spreadsheet ID
        campaign_id: Optional specific campaign to sync

    Returns:
        Sync results summary
    """
    # Get config from environment if not provided
    api_key = instantly_api_key or os.getenv("INSTANTLY_API_KEY")
    creds_file = credentials_file or os.getenv(
        "GOOGLE_CREDENTIALS_FILE",
        "config/google_credentials.json"
    )
    sheet_id = spreadsheet_id or os.getenv(
        "SPREADSHEET_ID",
        "1ZdhkP_Hq-340eVEOS-RKwHGjDaX0vNVP6vO48XzkOx8"
    )

    if not api_key:
        raise ValueError("INSTANTLY_API_KEY not provided")

    # Initialize CRM
    crm = GoogleSheetsCRM(
        credentials_file=creds_file,
        spreadsheet_id=sheet_id,
        sheet_name="Leads"
    )

    # Run sync
    syncer = InstantlySyncer(api_key, crm, campaign_id)
    return syncer.sync_all_leads()


if __name__ == "__main__":
    import json

    results = sync_from_instantly()
    print(json.dumps(results, indent=2))
