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


class InstantlySyncer:
    """Syncs replies from Instantly to Google Sheets CRM."""

    def __init__(
        self,
        instantly_api_key: str,
        crm: GoogleSheetsCRM,
        campaign_id: str = None
    ):
        """
        Initialize the reply syncer.

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

    def get_replied_leads(self, campaign_id: str) -> list[dict]:
        """
        Get all leads who have replied in a campaign.

        Args:
            campaign_id: Campaign ID to check

        Returns:
            List of leads with reply status
        """
        replied_leads = []

        # Get all leads from the campaign (paginate if needed)
        offset = 0
        limit = 100

        while True:
            # Instantly API uses skip/limit for pagination
            result = self.instantly._make_request(
                "GET",
                "lead/list",
                params={
                    "campaign_id": campaign_id,
                    "limit": limit,
                    "skip": offset
                }
            )

            if not result:
                break

            leads = result.get("leads", []) if isinstance(result, dict) else result

            if not leads:
                break

            for lead in leads:
                # Check if lead has replied
                # Instantly marks leads with status or reply_status
                status = lead.get("status", "").lower()
                has_replied = (
                    status == "replied" or
                    lead.get("replied", False) or
                    lead.get("reply_count", 0) > 0
                )

                if has_replied:
                    replied_leads.append({
                        "email": lead.get("email"),
                        "replied_at": lead.get("replied_at") or lead.get("last_reply_at"),
                        "reply_text": lead.get("reply_text", ""),
                        "status": status,
                    })

            # Check if we got fewer than limit, meaning no more pages
            if len(leads) < limit:
                break

            offset += limit

        return replied_leads

    def sync_replies(self) -> dict:
        """
        Sync all replies from Instantly to CRM.

        Returns:
            Summary of sync results
        """
        results = {
            "campaigns_checked": 0,
            "replies_found": 0,
            "crm_updated": 0,
            "already_synced": 0,
            "not_in_crm": 0,
            "errors": []
        }

        campaigns = self.get_campaigns()
        results["campaigns_checked"] = len(campaigns)

        logger.info("Starting reply sync", campaigns=len(campaigns))

        for campaign in campaigns:
            campaign_id = campaign.get("id")
            campaign_name = campaign.get("name", "Unknown")

            logger.info("Checking campaign", name=campaign_name, id=campaign_id)

            try:
                replied_leads = self.get_replied_leads(campaign_id)
                results["replies_found"] += len(replied_leads)

                for lead in replied_leads:
                    email = lead.get("email")
                    if not email:
                        continue

                    # Find lead in CRM
                    crm_lead = self.crm.find_lead_by_email(email)

                    if not crm_lead:
                        logger.debug("Lead not in CRM", email=email)
                        results["not_in_crm"] += 1
                        continue

                    # Check if already marked as replied
                    if crm_lead.get("response"):
                        logger.debug("Already synced", email=email)
                        results["already_synced"] += 1
                        continue

                    # Update CRM with reply info
                    lead_id = crm_lead.get("id")
                    reply_text = lead.get("reply_text") or f"Replied on {lead.get('replied_at', 'unknown date')}"

                    success = self.crm.mark_response_received(lead_id, reply_text)

                    if success:
                        logger.info("Updated CRM with reply", email=email, lead_id=lead_id)
                        results["crm_updated"] += 1
                    else:
                        results["errors"].append(f"Failed to update {email}")

            except Exception as e:
                error_msg = f"Error syncing campaign {campaign_name}: {str(e)}"
                logger.error(error_msg)
                results["errors"].append(error_msg)

        logger.info(
            "Reply sync complete",
            replies_found=results["replies_found"],
            crm_updated=results["crm_updated"],
            already_synced=results["already_synced"]
        )

        return results


def sync_replies_from_instantly(
    instantly_api_key: str = None,
    credentials_file: str = None,
    spreadsheet_id: str = None,
    campaign_id: str = None
) -> dict:
    """
    Convenience function to sync replies.

    Args:
        instantly_api_key: Instantly API key (or from env INSTANTLY_API_KEY)
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
    syncer = ReplySyncer(api_key, crm, campaign_id)
    return syncer.sync_replies()


if __name__ == "__main__":
    # Run sync when executed directly
    import json

    results = sync_replies_from_instantly()
    print(json.dumps(results, indent=2))
