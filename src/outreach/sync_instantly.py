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
        self.instantly = InstantlyClient(instantly_api_key)
        self.crm = crm
        self.campaign_id = campaign_id

    def get_campaigns(self) -> list[dict]:
        """Get all campaigns or the specific one configured."""
        if self.campaign_id:
            campaign = self.instantly.get_campaign(self.campaign_id)
            return [campaign] if campaign else []
        return self.instantly.list_campaigns()

    def _determine_emails_sent(self, lead: dict) -> int:
        """Determine how many emails have been sent based on Instantly lead data."""
        status = lead.get("status", 0)

        # Not started = 0 emails
        if status == 0:
            return 0

        # Completed = all 4 emails sent
        if status == 3:
            return 4

        # Try to get exact step from available fields
        for field in ("lead_last_step", "last_step", "subsequence_index"):
            step = lead.get(field)
            if step is not None:
                try:
                    return int(step) + 1  # Steps are 0-indexed in Instantly
                except (ValueError, TypeError):
                    continue

        # Fallback: any active/non-zero status means at least 1 email sent
        return 1

    def sync_all_leads(self) -> dict:
        """Sync all lead data from Instantly to CRM."""
        results = {
            "campaigns_checked": 0,
            "leads_checked": 0,
            "crm_updated": 0,
            "replies_found": 0,
            "emails_sent_updated": 0,
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
                leads = self.instantly.list_leads(campaign_id, limit=100)
                results["leads_checked"] += len(leads)

                # Log raw fields from first lead for debugging step detection
                if leads:
                    sample = leads[0]
                    logger.info(
                        "Sample Instantly lead fields",
                        campaign=campaign_name,
                        lead_keys=sorted(list(sample.keys())),
                        sample_fields={
                            k: sample.get(k)
                            for k in [
                                "status", "lead_last_step", "last_step",
                                "subsequence_index", "email_open_count",
                                "email_click_count", "email_reply_count"
                            ] if k in sample
                        }
                    )

                for lead in leads:
                    email = lead.get("email")
                    if not email:
                        continue

                    emails_sent = self._determine_emails_sent(lead)

                    sync_data = {
                        "opens": lead.get("email_open_count", 0),
                        "clicks": lead.get("email_click_count", 0),
                        "instantly_status": INSTANTLY_STATUS.get(
                            lead.get("status", 0),
                            f"Unknown ({lead.get('status')})"
                        ),
                        "emails_sent_count": emails_sent
                    }

                    if emails_sent > 0:
                        results["emails_sent_updated"] += 1

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
            replies_found=results["replies_found"],
            emails_sent_updated=results["emails_sent_updated"]
        )

        return results


def sync_from_instantly(
    instantly_api_key: str = None,
    credentials_file: str = None,
    spreadsheet_id: str = None,
    campaign_id: str = None
) -> dict:
    """Convenience function to sync from Instantly."""
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
