"""
Instantly.ai API Client
========================
Manages campaigns and sends emails via Instantly.
"""

import requests
from typing import Optional
import structlog

logger = structlog.get_logger()


class InstantlyClient:
    """Client for Instantly.ai API V2."""

    BASE_URL = "https://api.instantly.ai/api/v2"

    def __init__(self, api_key: str):
        """
        Initialize the Instantly client.

        Args:
            api_key: Instantly API key (Bearer token)
        """
        self.api_key = api_key
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }

    def _make_request(
        self,
        method: str,
        endpoint: str,
        data: dict = None,
        params: dict = None
    ) -> Optional[dict]:
        """Make an API request using V2 Bearer authentication."""
        url = f"{self.BASE_URL}/{endpoint}"

        try:
            if method == "GET":
                response = requests.get(url, headers=self.headers, params=params, timeout=30)
            elif method == "POST":
                response = requests.post(url, headers=self.headers, json=data, params=params, timeout=30)
            else:
                raise ValueError(f"Unsupported method: {method}")

            response.raise_for_status()
            return response.json() if response.content else {}

        except requests.RequestException as e:
            logger.error(
                "Instantly API request failed",
                endpoint=endpoint,
                error=str(e)
            )
            return None

    def list_campaigns(self) -> list[dict]:
        """List all campaigns."""
        result = self._make_request("GET", "campaigns")
        # V2 returns {items: [...], next_starting_after: ...}
        if result and "items" in result:
            return result["items"]
        return result if result else []

    def get_campaign(self, campaign_id: str) -> Optional[dict]:
        """Get campaign details."""
        return self._make_request("GET", f"campaigns/{campaign_id}")

    def create_campaign(self, name: str) -> Optional[dict]:
        """
        Create a new campaign.

        Args:
            name: Campaign name

        Returns:
            Campaign data or None
        """
        data = {"name": name}
        result = self._make_request("POST", "campaigns", data=data)

        if result:
            logger.info("Campaign created", name=name, id=result.get("id"))

        return result

    def add_leads_to_campaign(
        self,
        campaign_id: str,
        leads: list[dict]
    ) -> Optional[dict]:
        """
        Add leads to a campaign.

        Args:
            campaign_id: Campaign ID
            leads: List of lead dictionaries with email, first_name, last_name, etc.

        Returns:
            API response or None
        """
        results = []

        for lead in leads:
            # Extract first_name and last_name from contact_name if not provided
            contact_name = lead.get("contact_name", "")
            name_parts = contact_name.split() if contact_name else []

            first_name = lead.get("first_name") or (name_parts[0] if name_parts else "")
            last_name = lead.get("last_name") or (" ".join(name_parts[1:]) if len(name_parts) > 1 else "")

            # V2 API uses flat structure, one lead at a time
            data = {
                "campaign": campaign_id,
                "email": lead.get("email"),
                "first_name": first_name,
                "last_name": last_name,
                "company_name": lead.get("company_name") or lead.get("company", ""),
                "website": lead.get("website", ""),
                "phone": lead.get("phone", ""),
            }

            # Add custom variables via custom_variables object (required for Instantly V2 API)
            custom_vars = {}
            if lead.get("personalized_opener"):
                custom_vars["personalized_opener"] = lead["personalized_opener"]
            if lead.get("specific_pain_point"):
                custom_vars["specific_pain_point"] = lead["specific_pain_point"]
            if lead.get("industry_specific_insight"):
                custom_vars["industry_specific_insight"] = lead["industry_specific_insight"]
            if lead.get("industry"):
                custom_vars["industry"] = lead["industry"]
            if lead.get("city"):
                custom_vars["city"] = lead["city"]

            if custom_vars:
                data["custom_variables"] = custom_vars

            # Log payload for debugging field mapping issues
            logger.info(
                "Instantly lead payload",
                campaign_id=campaign_id,
                email=data.get("email"),
                payload_keys=sorted(list(data.keys()))
            )

            result = self._make_request("POST", "leads", data=data)

            if result:
                results.append(result)
                logger.info(
                    "Lead added to campaign",
                    campaign_id=campaign_id,
                    email=lead.get("email")
                )

        return results if results else None

    def get_campaign_analytics(self, campaign_id: str) -> Optional[dict]:
        """Get campaign analytics."""
        return self._make_request("GET", f"campaigns/{campaign_id}/analytics")

    def list_leads(self, campaign_id: str = None, limit: int = 100) -> list[dict]:
        """List leads in a campaign. V2 uses POST /leads/list."""
        data = {"limit": limit}
        if campaign_id:
            data["campaign_id"] = campaign_id

        result = self._make_request("POST", "leads/list", data=data)

        # V2 returns {items: [...]}
        if result and "items" in result:
            return result["items"]
        return result.get("leads", []) if result else []

    def get_lead_status(self, email: str, campaign_id: str = None) -> Optional[dict]:
        """Get status of a specific lead."""
        params = {"email": email}
        if campaign_id:
            params["campaign_id"] = campaign_id
        return self._make_request("GET", "leads", params=params)

    def pause_campaign(self, campaign_id: str) -> bool:
        """Pause a campaign."""
        result = self._make_request(
            "POST",
            f"campaigns/{campaign_id}/pause"
        )
        return result is not None

    def resume_campaign(self, campaign_id: str) -> bool:
        """Resume/activate a campaign."""
        result = self._make_request(
            "POST",
            f"campaigns/{campaign_id}/activate"
        )
        return result is not None

    def set_campaign_schedule(
        self,
        campaign_id: str,
        days: list[int] = None,  # 0=Sunday, 1=Monday, etc.
        start_hour: int = 9,
        end_hour: int = 17,
        timezone: str = "America/New_York"
    ) -> Optional[dict]:
        """
        Set campaign sending schedule.

        Args:
            campaign_id: Campaign ID
            days: Days to send (0=Sunday through 6=Saturday)
            start_hour: Hour to start sending (24h format)
            end_hour: Hour to stop sending (24h format)
            timezone: Timezone for the schedule
        """
        if days is None:
            days = [1, 2, 3, 4, 5]  # Monday to Friday

        data = {
            "campaign_id": campaign_id,
            "schedule": {
                "days": days,
                "start_hour": start_hour,
                "end_hour": end_hour,
                "timezone": timezone
            }
        }

        return self._make_request("POST", "campaign/update/schedule", data=data)

    def set_campaign_sequences(
        self,
        campaign_id: str,
        sequences: list[dict]
    ) -> Optional[dict]:
        """
        Set email sequences for a campaign.

        Args:
            campaign_id: Campaign ID
            sequences: List of sequence steps with subject and body

        Each sequence item should have:
        - subject: Email subject
        - body: Email body (can include {{variables}})
        - delay: Days to wait before sending (0 for first email)
        """
        data = {
            "campaign_id": campaign_id,
            "sequences": sequences
        }

        return self._make_request("POST", "campaign/update/sequences", data=data)


def setup_campaign(
    api_key: str,
    campaign_name: str,
    email_sequences: list[dict],
    schedule: dict = None
) -> Optional[str]:
    """
    Set up a complete campaign in Instantly.

    Args:
        api_key: Instantly API key
        campaign_name: Name for the campaign
        email_sequences: List of email sequence steps
        schedule: Sending schedule configuration

    Returns:
        Campaign ID if successful
    """
    client = InstantlyClient(api_key)

    # Check if campaign already exists
    campaigns = client.list_campaigns()
    for campaign in campaigns:
        if campaign.get("name") == campaign_name:
            logger.info("Using existing campaign", name=campaign_name)
            return campaign.get("id")

    # Create new campaign
    result = client.create_campaign(campaign_name)
    if not result:
        logger.error("Failed to create campaign")
        return None

    campaign_id = result.get("id")

    # Set up sequences
    if email_sequences:
        client.set_campaign_sequences(campaign_id, email_sequences)

    # Set up schedule
    if schedule:
        client.set_campaign_schedule(
            campaign_id,
            days=schedule.get("days"),
            start_hour=schedule.get("start_hour", 9),
            end_hour=schedule.get("end_hour", 17),
            timezone=schedule.get("timezone", "America/New_York")
        )

    logger.info("Campaign setup complete", campaign_id=campaign_id)
    return campaign_id
