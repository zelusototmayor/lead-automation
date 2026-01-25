"""
Apollo.io API - Lead Enrichment Module
=======================================
Enriches company data and finds decision-maker contacts.
"""

import requests
from typing import Optional
import structlog

logger = structlog.get_logger()


class ApolloClient:
    """Client for Apollo.io API."""

    BASE_URL = "https://api.apollo.io/v1"

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.headers = {
            "Content-Type": "application/json",
            "Cache-Control": "no-cache",
            "X-Api-Key": api_key
        }

    def search_organizations(
        self,
        company_name: str,
        domain: str = None,
        location: str = None
    ) -> Optional[dict]:
        """
        Search for an organization in Apollo's database.

        Args:
            company_name: Name of the company
            domain: Company website domain (optional but recommended)
            location: City/country for filtering

        Returns:
            Organization data or None
        """
        url = f"{self.BASE_URL}/organizations/search"

        payload = {
            "q_organization_name": company_name,
            "page": 1,
            "per_page": 5
        }

        if domain:
            # Clean domain
            domain = domain.replace("https://", "").replace("http://", "").replace("www.", "").split("/")[0]
            payload["q_organization_domains"] = domain

        try:
            response = requests.post(url, json=payload, headers=self.headers, timeout=30)
            response.raise_for_status()
            data = response.json()

            organizations = data.get("organizations", [])
            if not organizations:
                logger.debug("No organization found", company=company_name)
                return None

            # Return best match (first result)
            org = organizations[0]
            return {
                "apollo_id": org.get("id"),
                "name": org.get("name"),
                "domain": org.get("primary_domain"),
                "industry": org.get("industry"),
                "employee_count": org.get("estimated_num_employees"),
                "employee_range": org.get("employee_range"),
                "founded_year": org.get("founded_year"),
                "linkedin_url": org.get("linkedin_url"),
                "description": org.get("short_description"),
                "technologies": org.get("technologies", []),
                "keywords": org.get("keywords", []),
                "city": org.get("city"),
                "state": org.get("state"),
                "country": org.get("country")
            }

        except requests.RequestException as e:
            logger.error("Apollo organization search failed", error=str(e))
            return None

    def find_contacts(
        self,
        company_domain: str = None,
        company_name: str = None,
        titles: list[str] = None,
        seniority: list[str] = None,
        limit: int = 3
    ) -> list[dict]:
        """
        Find contacts at a company.

        Args:
            company_domain: Company website domain
            company_name: Company name (used if domain not available)
            titles: Job titles to search for
            seniority: Seniority levels (e.g., ["owner", "founder", "c_suite", "director"])
            limit: Maximum contacts to return

        Returns:
            List of contact dictionaries
        """
        url = f"{self.BASE_URL}/people/search"

        # Default to decision-maker titles for agencies
        if not titles:
            titles = [
                "CEO", "Founder", "Owner", "Managing Director",
                "Director", "President", "Partner", "Principal",
                "Head of Operations", "COO", "General Manager"
            ]

        if not seniority:
            seniority = ["owner", "founder", "c_suite", "vp", "director", "manager"]

        payload = {
            "page": 1,
            "per_page": limit,
            "person_seniorities": seniority
        }

        if company_domain:
            domain = company_domain.replace("https://", "").replace("http://", "").replace("www.", "").split("/")[0]
            payload["q_organization_domains"] = domain
        elif company_name:
            payload["q_organization_name"] = company_name
        else:
            logger.warning("No company identifier provided")
            return []

        try:
            response = requests.post(url, json=payload, headers=self.headers, timeout=30)
            response.raise_for_status()
            data = response.json()

            contacts = []
            for person in data.get("people", [])[:limit]:
                contact = {
                    "apollo_id": person.get("id"),
                    "first_name": person.get("first_name"),
                    "last_name": person.get("last_name"),
                    "full_name": person.get("name"),
                    "email": person.get("email"),
                    "email_status": person.get("email_status"),
                    "title": person.get("title"),
                    "seniority": person.get("seniority"),
                    "linkedin_url": person.get("linkedin_url"),
                    "city": person.get("city"),
                    "state": person.get("state"),
                    "country": person.get("country"),
                    "company_name": person.get("organization", {}).get("name"),
                    "company_domain": person.get("organization", {}).get("primary_domain")
                }

                # Only include contacts with verified emails
                if contact["email"] and contact["email_status"] == "verified":
                    contacts.append(contact)
                elif contact["email"]:
                    # Include unverified but log it
                    contact["email_verified"] = False
                    contacts.append(contact)
                    logger.debug(
                        "Contact email not verified",
                        name=contact["full_name"],
                        status=contact["email_status"]
                    )

            logger.info(
                "Found contacts",
                company=company_domain or company_name,
                count=len(contacts)
            )
            return contacts

        except requests.RequestException as e:
            logger.error("Apollo contact search failed", error=str(e))
            return []

    def enrich_email(self, email: str) -> Optional[dict]:
        """
        Enrich a single email address.

        Args:
            email: Email address to enrich

        Returns:
            Person data or None
        """
        url = f"{self.BASE_URL}/people/match"

        payload = {
            "email": email
        }

        try:
            response = requests.post(url, json=payload, headers=self.headers, timeout=30)
            response.raise_for_status()
            data = response.json()

            person = data.get("person")
            if not person:
                return None

            return {
                "first_name": person.get("first_name"),
                "last_name": person.get("last_name"),
                "full_name": person.get("name"),
                "title": person.get("title"),
                "linkedin_url": person.get("linkedin_url"),
                "company_name": person.get("organization", {}).get("name"),
                "company_domain": person.get("organization", {}).get("primary_domain")
            }

        except requests.RequestException as e:
            logger.error("Apollo email enrichment failed", error=str(e))
            return None


def enrich_lead(
    api_key: str,
    company_name: str,
    website: str = None,
    city: str = None
) -> dict:
    """
    Enrich a lead with Apollo data.

    Args:
        api_key: Apollo API key
        company_name: Company name
        website: Company website
        city: Company city

    Returns:
        Enriched lead data
    """
    client = ApolloClient(api_key)

    result = {
        "company_name": company_name,
        "website": website,
        "enrichment_source": "apollo",
        "contacts": []
    }

    # Search for organization
    org_data = client.search_organizations(
        company_name=company_name,
        domain=website,
        location=city
    )

    if org_data:
        result.update({
            "industry": org_data.get("industry"),
            "employee_count": org_data.get("employee_count"),
            "employee_range": org_data.get("employee_range"),
            "founded_year": org_data.get("founded_year"),
            "linkedin_url": org_data.get("linkedin_url"),
            "description": org_data.get("description"),
            "technologies": org_data.get("technologies", []),
            "keywords": org_data.get("keywords", [])
        })

    # Find decision-maker contacts
    contacts = client.find_contacts(
        company_domain=website,
        company_name=company_name,
        limit=3
    )

    if contacts:
        result["contacts"] = contacts
        # Set primary contact (first one with verified email)
        for contact in contacts:
            if contact.get("email"):
                result["primary_contact"] = contact
                break

    logger.info(
        "Lead enriched",
        company=company_name,
        has_contacts=len(contacts) > 0
    )
    return result
