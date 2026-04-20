"""
Instantly SuperSearch Enrichment — Apollo Replacement
======================================================
Uses Instantly's SuperSearch API to find decision-maker contacts
with verified emails. Drop-in replacement for Apollo enrichment.

Credit cost: 1-4 per verified email, 0 if not found.
Waterfall enrichment: 7+ providers for high email discovery rate.

API: POST /api/v2/supersearch-enrichment/enrich-leads-from-supersearch
"""

import time
import requests
from typing import Optional
import structlog

logger = structlog.get_logger()

# AEC-specific seniority targets
AEC_SENIORITIES = ["owner", "founder", "c_suite", "vp", "director"]
AEC_TITLES = [
    "Principal", "Managing Principal", "President", "CEO",
    "Owner", "Founder", "Partner", "Managing Partner",
    "Director of Business Development", "VP Business Development",
    "Director", "Vice President",
]


class InstantlyEnrichmentClient:
    """Instantly SuperSearch client for lead enrichment."""

    BASE_URL = "https://api.instantly.ai/api/v2"

    def __init__(self, api_key: str, credit_budget: int = 0):
        self.api_key = api_key
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        self._credit_budget = credit_budget
        self._credits_used = 0
        self._credits_exhausted = False
        # Tracking
        self._enrichments_attempted = 0
        self._enrichments_with_email = 0
        self._enrichments_no_email = 0
        self._orgs_skipped = 0

    def _check_budget(self) -> bool:
        if self._credit_budget and self._credits_used >= self._credit_budget:
            if not self._credits_exhausted:
                logger.warning("Instantly credit budget reached",
                               budget=self._credit_budget, used=self._credits_used)
                self._credits_exhausted = True
            return True
        return False

    def get_credit_summary(self) -> dict:
        ratio = (
            self._credits_used / self._enrichments_with_email
            if self._enrichments_with_email > 0 else float("inf")
        )
        return {
            "credits_used": self._credits_used,
            "credits_budget": self._credit_budget,
            "enrichments_attempted": self._enrichments_attempted,
            "enrichments_with_email": self._enrichments_with_email,
            "enrichments_no_email": self._enrichments_no_email,
            "orgs_skipped": self._orgs_skipped,
            "credits_per_lead": round(ratio, 2),
        }

    def search_and_enrich(
        self,
        search_filters: dict,
        limit: int = 1,
    ) -> list[dict]:
        """
        Call Instantly SuperSearch to find and enrich leads.

        Returns list of enriched contact dicts.
        """
        if self._credits_exhausted or self._check_budget():
            return []

        url = f"{self.BASE_URL}/supersearch-enrichment/enrich-leads-from-supersearch"

        payload = {
            "search_filters": search_filters,
            "limit": limit,
            "work_email_enrichment": True,
        }

        for attempt in range(3):
            try:
                resp = requests.post(
                    url, headers=self.headers, json=payload, timeout=60
                )

                if resp.status_code == 429 and attempt < 2:
                    wait = (attempt + 1) * 5
                    logger.warning("Rate limited, retrying", wait=wait)
                    time.sleep(wait)
                    continue

                resp.raise_for_status()
                data = resp.json()

                # Track credits (estimate — Instantly doesn't return exact credit cost)
                leads = self._extract_leads(data)
                if leads:
                    # Estimate 1-2 credits per enriched lead
                    estimated_credits = len(leads) * 2
                    self._credits_used += estimated_credits

                return leads

            except requests.RequestException as e:
                if attempt < 2 and "429" in str(e):
                    time.sleep((attempt + 1) * 5)
                    continue

                status = getattr(e.response, 'status_code', None) if hasattr(e, 'response') else None
                body = ""
                if hasattr(e, 'response') and e.response is not None:
                    try:
                        body = e.response.text[:500]
                    except Exception:
                        pass

                # Check for credit/plan errors
                if status in (402, 403) or "credit" in body.lower() or "plan" in body.lower():
                    self._credits_exhausted = True
                    logger.error("Instantly credits/plan issue", status=status, body=body)

                logger.error("Instantly SuperSearch failed",
                             error=str(e), status=status, body=body)
                return []

        return []

    def _extract_leads(self, data: dict) -> list[dict]:
        """Extract lead contacts from the API response.

        The response format may vary — handle multiple structures.
        """
        # Try common response shapes
        leads = data.get("leads", [])
        if not leads:
            leads = data.get("items", [])
        if not leads:
            leads = data.get("data", [])
        if not leads and data.get("email"):
            leads = [data]

        results = []
        for lead in leads:
            if not isinstance(lead, dict):
                continue

            email = (
                lead.get("email")
                or lead.get("work_email")
                or lead.get("business_email")
                or ""
            )

            name = (
                lead.get("name")
                or lead.get("full_name")
                or ""
            )
            if not name:
                first = lead.get("first_name", "")
                last = lead.get("last_name", "")
                name = f"{first} {last}".strip()

            results.append({
                "email": email,
                "full_name": name,
                "first_name": lead.get("first_name", ""),
                "last_name": lead.get("last_name", ""),
                "title": lead.get("title", lead.get("job_title", "")),
                "linkedin_url": lead.get("linkedin_url", lead.get("linkedin", "")),
                "phone": lead.get("phone", lead.get("phone_number", "")),
                "city": lead.get("city", ""),
                "state": lead.get("state", ""),
                "country": lead.get("country", ""),
                "company_name": lead.get("company_name", lead.get("company", "")),
                "company_domain": lead.get("company_domain", lead.get("domain", "")),
                "industry": lead.get("industry", ""),
                "employee_count": lead.get("employee_count", lead.get("company_size", "")),
                "seniority": lead.get("seniority", ""),
            })

        return results

    def find_contact_at_company(
        self,
        company_name: str,
        website: str = None,
        city: str = None,
        country: str = None,
        seniority: list[str] = None,
        job_titles: list[str] = None,
    ) -> Optional[dict]:
        """
        Find a decision-maker contact at a specific company.

        This is the main method that replaces Apollo's enrich_lead().
        Returns a single contact dict with email, or None.
        """
        if self._credits_exhausted or self._check_budget():
            return None

        self._enrichments_attempted += 1

        filters = {}

        # Company filter — SuperSearch V2 requires object format
        if company_name:
            filters["company_name"] = {"include": [company_name]}

        if website:
            domain = (website.replace("https://", "").replace("http://", "")
                      .replace("www.", "").split("/")[0])
            filters["company_domain"] = {"include": [domain]}

        # Location — SuperSearch V2 requires array of objects
        if city and country:
            filters["locations"] = [{"city": city, "country": country}]
        elif country:
            filters["locations"] = [{"country": country}]

        # Seniority & titles
        if seniority:
            filters["seniority"] = seniority
        if job_titles:
            filters["job_titles"] = job_titles

        filters["one_per_company"] = True

        leads = self.search_and_enrich(filters, limit=1)

        if leads and leads[0].get("email"):
            self._enrichments_with_email += 1
            contact = leads[0]
            logger.info("Contact found via Instantly",
                        company=company_name,
                        email=contact["email"],
                        title=contact.get("title", ""))
            return contact
        else:
            self._enrichments_no_email += 1
            logger.debug("No contact found via Instantly", company=company_name)
            return None


def enrich_lead_instantly(
    instantly_api_key: str,
    company_name: str,
    website: str = None,
    city: str = None,
    country: str = None,
    client: InstantlyEnrichmentClient = None,
) -> dict:
    """
    Enrich a lead using Instantly SuperSearch.
    Drop-in replacement for apollo.enrich_lead().

    Returns dict with same structure as the Apollo version.
    """
    if client is None:
        client = InstantlyEnrichmentClient(instantly_api_key)

    result = {
        "company_name": company_name,
        "website": website,
        "enrichment_source": "instantly_supersearch",
        "contacts": [],
    }

    contact = client.find_contact_at_company(
        company_name=company_name,
        website=website,
        city=city,
        country=country or "US",
        seniority=AEC_SENIORITIES,
        job_titles=AEC_TITLES,
    )

    if contact:
        result["contacts"] = [contact]
        result["primary_contact"] = contact
        # Backfill org data from the contact (Instantly includes some)
        result["industry"] = contact.get("industry", "")
        result["employee_count"] = contact.get("employee_count", "")
        result["description"] = ""

    return result
