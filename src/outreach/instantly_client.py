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
    """Client for Instantly.ai API."""

    BASE_URL = "https://api.instantly.ai/api/v1"

    def __init__(self, api_key: str):
        """
        Initialize the Instantly client.

        Args:
            api_key: Instantly API key
        """
        self.api_key = api_key

    def _make_request(
        self,
        method: str,
        endpoint: str,
        data: dict = None,
        params: dict = None
    ) -> Optional[dict]:
        """Make an API request."""
        url = f"{self.BASE_URL}/{endpoint}"

        # Add API key to params
        if params is None:
            params = {}
        params["api_key"] = self.api_key

        try:
            if method == "GET":
                response = requests.get(url, params=params, timeout=30)
            elif method == "POST":
                response = requests.post(url, json=data, params=params, timeout=30)
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
        result = self._make_request("GET", "campaign/list")
        return result if result else []

    def get_campaign(self, campaign_id: str) -> Optional[dict]:
        """Get campaign details."""
        return self._make_request("GET", "campaign/get", params={"campaign_id": campaign_id})

    def create_campaign(self, name: str) -> Optional[dict]:
        """
        Create a new campaign.

        Args:
            name: Campaign name

        Returns:
            Campaign data or None
        """
        data = {"name": name}
        result = self._make_request("POST", "campaign/create", data=data)

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
        # Format leads for Instantly
        formatted_leads = []
        for lead in leads:
            formatted_lead = {
                "email": lead.get("email"),
                "first_name": lead.get("first_name", lead.get("contact_name", "").split()[0] if lead.get("contact_name") else ""),
                "last_name": lead.get("last_name", ""),
                "company_name": lead.get("company", ""),
                "website": lead.get("website", ""),
                "phone": lead.get("phone", ""),
            }

            # Add custom variables for personalization
            if lead.get("personalized_opener"):
                formatted_lead["personalized_opener"] = lead["personalized_opener"]
            if lead.get("specific_pain_point"):
                formatted_lead["specific_pain_point"] = lead["specific_pain_point"]
            if lead.get("industry_specific_insight"):
                formatted_lead["industry_specific_insight"] = lead["industry_specific_insight"]
            if lead.get("industry"):
                formatted_lead["industry"] = lead["industry"]
            if lead.get("city"):
                formatted_lead["city"] = lead["city"]

            formatted_leads.append(formatted_lead)

        data = {
            "campaign_id": campaign_id,
            "leads": formatted_leads,
            "skip_if_in_workspace": True  # Avoid duplicates
        }

        result = self._make_request("POST", "lead/add", data=data)

        if result:
            logger.info(
                "Leads added to campaign",
                campaign_id=campaign_id,
                count=len(leads)
            )

        return result

    def get_campaign_analytics(self, campaign_id: str) -> Optional[dict]:
        """Get campaign analytics."""
        return self._make_request("GET", "analytics/campaign/summary", params={"campaign_id": campaign_id})

    def list_leads(self, campaign_id: str, limit: int = 100) -> list[dict]:
        """List leads in a campaign."""
        result = self._make_request(
            "GET",
            "lead/list",
            params={"campaign_id": campaign_id, "limit": limit}
        )
        return result.get("leads", []) if result else []

    def get_lead_status(self, email: str, campaign_id: str = None) -> Optional[dict]:
        """Get status of a specific lead."""
        params = {"email": email}
        if campaign_id:
            params["campaign_id"] = campaign_id
        return self._make_request("GET", "lead/get", params=params)

    def pause_campaign(self, campaign_id: str) -> bool:
        """Pause a campaign."""
        result = self._make_request(
            "POST",
            "campaign/update/status",
            data={"campaign_id": campaign_id, "status": "paused"}
        )
        return result is not None

    def resume_campaign(self, campaign_id: str) -> bool:
        """Resume a campaign."""
        result = self._make_request(
            "POST",
            "campaign/update/status",
            data={"campaign_id": campaign_id, "status": "active"}
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
