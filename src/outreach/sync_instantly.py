"""
Instantly CRM Sync
==================
Two-phase sync: lead roster from POST /leads/list, then selective
GET /emails for leads that need detailed email activity data.
Batch-writes all updates to Google Sheets CRM.
"""

import os
import time
from collections import Counter
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
    10: "Closed",
}

# Status rank for protection — never downgrade a higher-rank status
STATUS_RANK = {
    "New": 0,
    "Queued": 1,
    "Contacted": 2,
    "Replied": 3,
    "Interested": 4,
    "Meeting Booked": 5,
    "Won": 6,
    "Lost": 3,  # same as replied — don't overwrite with Contacted
}

# Rate limit between GET /emails calls (seconds)
EMAIL_FETCH_DELAY = 1.0


class InstantlySyncer:
    """Syncs lead data from Instantly to Google Sheets CRM."""

    def __init__(
        self,
        instantly_api_key: str,
        crm: GoogleSheetsCRM,
        campaign_id: str = None,
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

    def sync_all_leads(self) -> dict:
        """Two-phase sync: roster → selective email fetch → batch CRM write."""
        results = {
            "campaigns_checked": 0,
            "leads_checked": 0,
            "crm_updated": 0,
            "replies_found": 0,
            "emails_fetched": 0,
            "not_in_crm": 0,
            "skipped_no_change": 0,
            "errors": [],
        }

        campaigns = self.get_campaigns()
        results["campaigns_checked"] = len(campaigns)
        logger.info("Starting Instantly sync", campaigns=len(campaigns))

        for campaign in campaigns:
            campaign_id = campaign.get("id")
            campaign_name = campaign.get("name", "Unknown")
            logger.info("Syncing campaign", name=campaign_name)

            try:
                self._sync_campaign(campaign_id, campaign_name, results)
            except Exception as e:
                import traceback
                tb = traceback.format_exc()
                error_msg = f"Error syncing campaign {campaign_name}: {str(e)}"
                logger.error(error_msg, traceback=tb)
                results["errors"].append(error_msg)

        logger.info(
            "Instantly sync complete",
            leads_checked=results["leads_checked"],
            crm_updated=results["crm_updated"],
            replies_found=results["replies_found"],
            emails_fetched=results["emails_fetched"],
        )
        return results

    def _sync_campaign(self, campaign_id: str, campaign_name: str, results: dict):
        """Sync a single campaign: roster → email fetch → batch write."""

        # Phase A: Get lead roster (cheap — paginated POST /leads/list)
        leads = self.instantly.list_leads(campaign_id, limit=100)
        results["leads_checked"] += len(leads)

        if not leads:
            logger.info("No leads in campaign", campaign=campaign_name)
            return

        # Log status distribution
        status_dist = Counter(lead.get("status", 0) for lead in leads)
        logger.info(
            "Instantly lead status distribution",
            campaign=campaign_name,
            total=len(leads),
            distribution={
                INSTANTLY_STATUS.get(s, f"Unknown({s})"): c
                for s, c in status_dist.items()
            },
        )

        # Phase B: For each lead, check CRM and decide if email fetch needed
        batch_updates = []

        for lead in leads:
            email = lead.get("email")
            if not email or not isinstance(email, str):
                continue

            crm_lead = self.crm.find_lead_by_email(email)
            if not crm_lead:
                results["not_in_crm"] += 1
                continue

            # Decide if we need detailed email activity
            activity = None
            if self._needs_email_fetch(lead, crm_lead):
                time.sleep(EMAIL_FETCH_DELAY)
                activity = self._get_email_activity(email, campaign_id)
                results["emails_fetched"] += 1

                if activity and activity["has_reply"]:
                    results["replies_found"] += 1

            # Build update dict
            update_data = self._build_sync_data(lead, crm_lead, activity)

            if update_data:
                batch_updates.append((crm_lead["id"], update_data))
            else:
                results["skipped_no_change"] += 1

        # Phase C: Batch write to CRM
        if batch_updates:
            updated = self.crm.batch_update_leads(batch_updates)
            results["crm_updated"] += updated
            logger.info(
                "Batch CRM update",
                campaign=campaign_name,
                updated=updated,
            )

    def _needs_email_fetch(self, instantly_lead: dict, crm_lead: dict) -> bool:
        """Decide if we need to call GET /emails for this lead.

        YES if:
        - reply_count > 0 but CRM has no response
        - lead has sent emails (last_step >= 0) but CRM missing email data
        - status >= 3 (Completed) but CRM doesn't show all 4 emails
        NO if:
        - status 0 (Not Started) — truly queued
        - CRM already has matching data
        """
        status = instantly_lead.get("status", 0)

        # Reply detected but CRM doesn't have response text
        reply_count = instantly_lead.get("email_reply_count", 0) or instantly_lead.get("reply_count", 0)
        if reply_count and reply_count > 0:
            crm_response = (crm_lead.get("response") or "").strip()
            if not crm_response or crm_response.startswith("Replied ("):
                return True

        # Status 0 (Not Started): truly queued, no emails sent
        if status == 0:
            return False

        # For Active/Paused leads: check if Instantly reports any sent emails
        # via last_completed_step or equivalent fields
        last_step = instantly_lead.get("lead_last_step") or instantly_lead.get("last_completed_step")
        if last_step is not None:
            try:
                step_num = int(last_step) + 1  # 0-indexed → 1-indexed
                # Check if CRM is missing data for steps that were sent
                for s in range(1, min(step_num, 4) + 1):
                    val = (crm_lead.get(f"email_{s}_sent") or "").strip()
                    if val in ("", "FALSE"):
                        return True
                return False
            except (ValueError, TypeError):
                pass

        # Status 1-2 (Active/Paused) without step info: check if CRM has email_1_sent
        if status in (1, 2):
            val = (crm_lead.get("email_1_sent") or "").strip()
            if val in ("", "FALSE"):
                # Active lead with no email_1_sent — likely needs fetch
                return True
            return False

        # Status 3 (Completed): should have all 4 emails
        if status == 3:
            for step in range(1, 5):
                val = (crm_lead.get(f"email_{step}_sent") or "").strip()
                if val in ("", "FALSE"):
                    return True
            return False

        # Status 4+ (Bounced, Unsubscribed, Replied, etc): need at least email 1
        val = (crm_lead.get("email_1_sent") or "").strip()
        if val in ("", "FALSE"):
            return True

        return False

    def _get_email_activity(self, email: str, campaign_id: str) -> Optional[dict]:
        """Parse GET /emails response into structured activity data."""
        emails = self.instantly.get_lead_emails(email, campaign_id)

        if not emails:
            return None

        steps_sent = {}
        has_reply = False
        reply_text = ""
        last_send_timestamp = ""

        for em in emails:
            ue_type = str(em.get("ue_type") or em.get("type") or "").lower()
            timestamp = em.get("timestamp_email") or em.get("timestamp") or em.get("created_at") or ""

            if ue_type == "received" or em.get("is_reply"):
                # This is a reply from the lead
                has_reply = True
                body = em.get("body") or em.get("text_body") or ""
                if body and (not reply_text or len(body) > len(reply_text)):
                    reply_text = body[:500]  # Cap at 500 chars for CRM
            else:
                # Outbound email — extract step number
                # Instantly V2 uses step IDs like "0_2_0" (variant_step_substep)
                step_raw = em.get("step") or em.get("subsequence_step") or em.get("stepID")
                if step_raw is not None:
                    try:
                        step_str = str(step_raw)
                        if "_" in step_str:
                            # Format: "variant_step_substep" → middle number is 0-indexed step
                            step_num = int(step_str.split("_")[1]) + 1
                        else:
                            step_num = int(step_str) + 1
                        ts_short = self._format_timestamp(timestamp)
                        if 1 <= step_num <= 4:
                            if step_num not in steps_sent or not steps_sent[step_num]:
                                steps_sent[step_num] = ts_short
                            if ts_short > last_send_timestamp:
                                last_send_timestamp = ts_short
                    except (ValueError, TypeError, IndexError):
                        pass

        return {
            "steps_sent": steps_sent,
            "total_sent": len(steps_sent),
            "has_reply": has_reply,
            "reply_text": reply_text,
            "last_send_timestamp": last_send_timestamp,
        }

    def _build_sync_data(self, lead: dict, crm_lead: dict, activity: Optional[dict]) -> dict:
        """Build the update dict for a lead. Returns empty dict if no changes needed."""
        updates = {}
        status = lead.get("status", 0)
        instantly_status_str = INSTANTLY_STATUS.get(status, f"Unknown ({status})")

        # Always update opens/clicks/instantly_status if changed
        opens = str(lead.get("email_open_count", 0) or 0)
        clicks = str(lead.get("email_click_count", 0) or 0)

        if opens != (crm_lead.get("opens") or "0"):
            updates["opens"] = opens
        if clicks != (crm_lead.get("clicks") or "0"):
            updates["clicks"] = clicks
        if instantly_status_str != (crm_lead.get("instantly_status") or ""):
            updates["instantly_status"] = instantly_status_str

        # Email activity from GET /emails
        if activity:
            for step_num, timestamp in activity["steps_sent"].items():
                if 1 <= step_num <= 4:
                    key = f"email_{step_num}_sent"
                    crm_val = (crm_lead.get(key) or "").strip()
                    if crm_val in ("", "FALSE", "TRUE"):
                        # Write timestamp (upgrade from TRUE or FALSE)
                        updates[key] = timestamp

            if activity["last_send_timestamp"]:
                updates["last_contact"] = activity["last_send_timestamp"]

            if activity["has_reply"] and activity["reply_text"]:
                updates["response"] = activity["reply_text"]
        else:
            # No email fetch — use roster step data if available
            if status == 0:
                # Truly not started — correct any false positives
                if crm_lead.get("email_1_sent") not in ("", "FALSE"):
                    updates["email_1_sent"] = "FALSE"
                    logger.warning(
                        "Correcting false email_1_sent for Not Started lead",
                        email=lead.get("email"),
                    )

        # Status upgrade with rank protection
        new_status = self._determine_crm_status(status, activity, crm_lead)
        current_status = crm_lead.get("status") or "New"

        if new_status and new_status != current_status:
            current_rank = STATUS_RANK.get(current_status, 0)
            new_rank = STATUS_RANK.get(new_status, 0)
            if new_rank >= current_rank:
                updates["status"] = new_status

        # No-op detection: skip if nothing changed
        return updates

    def _determine_crm_status(self, instantly_status: int, activity: Optional[dict], crm_lead: dict) -> str:
        """Map Instantly status + activity to CRM status string."""
        # Reply takes priority
        if activity and activity["has_reply"]:
            return "Replied"

        # Engagement statuses from Instantly
        if instantly_status in (7,):  # Interested
            return "Interested"
        if instantly_status in (9,):  # Meeting Booked
            return "Meeting Booked"
        if instantly_status in (10,):  # Closed
            return "Won"

        # Emails were sent (from email fetch) → Contacted
        if activity and activity["total_sent"] > 0:
            return "Contacted"

        # Status 0: truly not started
        if instantly_status == 0:
            return ""

        # Status 1-2 (Active/Paused): if email fetch found emails → already "Contacted" above.
        # If no fetch was done, check if CRM already shows contacted.
        if instantly_status in (1, 2):
            current = crm_lead.get("status") or "New"
            if current in ("New",):
                return "Queued"
            return ""

        # Bounced/Completed/Unsubscribed/Replied/Not Interested — at least 1 email went out
        if instantly_status in (3, 4, 5, 6, 8):
            return "Contacted"

        return ""

    def _format_timestamp(self, raw: str) -> str:
        """Parse various timestamp formats into 'YYYY-MM-DD HH:MM'."""
        if not raw:
            return ""
        for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                dt = datetime.strptime(raw, fmt)
                return dt.strftime("%Y-%m-%d %H:%M")
            except ValueError:
                continue
        # If it already looks like our format, return as-is
        if len(raw) >= 16 and raw[4] == "-" and raw[10] == " ":
            return raw[:16]
        return raw[:16] if len(raw) >= 16 else raw


def sync_from_instantly(
    instantly_api_key: str = None,
    credentials_file: str = None,
    spreadsheet_id: str = None,
    campaign_id: str = None,
) -> dict:
    """Convenience function to sync from Instantly."""
    api_key = instantly_api_key or os.getenv("INSTANTLY_API_KEY")
    creds_file = credentials_file or os.getenv(
        "GOOGLE_CREDENTIALS_FILE", "config/google_credentials.json"
    )
    sheet_id = spreadsheet_id or os.getenv(
        "SPREADSHEET_ID", "1ZdhkP_Hq-340eVEOS-RKwHGjDaX0vNVP6vO48XzkOx8"
    )

    if not api_key:
        raise ValueError("INSTANTLY_API_KEY not provided")

    crm = GoogleSheetsCRM(
        credentials_file=creds_file,
        spreadsheet_id=sheet_id,
        sheet_name="Leads",
    )

    syncer = InstantlySyncer(api_key, crm, campaign_id)
    return syncer.sync_all_leads()


if __name__ == "__main__":
    import json

    results = sync_from_instantly()
    print(json.dumps(results, indent=2))
