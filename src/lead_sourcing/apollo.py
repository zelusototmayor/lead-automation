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

    def __init__(self, api_key: str, credit_budget: int = 0):
        """
        Args:
            api_key: Apollo API key
            credit_budget: Max enrichment credits to use (0 = unlimited)
        """
        self.api_key = api_key
        self.headers = {
            "Content-Type": "application/json",
            "Cache-Control": "no-cache",
            "X-Api-Key": api_key
        }
        self._credits_exhausted = False
        self._credit_budget = credit_budget
        self._credits_used = 0

    @staticmethod
    def _extract_error_detail(exc: requests.RequestException) -> str:
        """Extract the response body from a request exception for logging."""
        if hasattr(exc, "response") and exc.response is not None:
            try:
                return str(exc.response.json())
            except Exception:
                return exc.response.text[:300]
        return ""

    def _check_credits_exhausted(self, exc: requests.RequestException) -> bool:
        """Check if the error is due to insufficient credits and flag it."""
        detail = self._extract_error_detail(exc)
        if "insufficient credits" in detail.lower():
            if not self._credits_exhausted:
                logger.error("Apollo credits exhausted — skipping remaining credit-consuming calls",
                             credits_used=self._credits_used)
                self._credits_exhausted = True
            return True
        return False

    def _check_budget(self) -> bool:
        """Return True if we've hit the credit budget and should stop."""
        if self._credit_budget and self._credits_used >= self._credit_budget:
            if not self._credits_exhausted:
                logger.warning("Apollo credit budget reached — stopping enrichment",
                               budget=self._credit_budget, credits_used=self._credits_used)
                self._credits_exhausted = True
            return True
        return False

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

        if self._credits_exhausted:
            return None

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
            self._check_credits_exhausted(e)
            detail = self._extract_error_detail(e)
            logger.error("Apollo organization search failed", error=str(e), response_detail=detail)
            return None

    def _search_people_free(
        self,
        company_domain: str = None,
        company_name: str = None,
        seniority: list[str] = None,
        limit: int = 6,
    ) -> list[dict]:
        """
        Search for people using the free mixed_people/api_search endpoint.
        Returns raw person dicts from Apollo (no credits consumed).
        """
        search_url = "https://api.apollo.io/api/v1/mixed_people/api_search"

        if not seniority:
            seniority = ["owner", "founder", "c_suite", "vp", "director"]

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
            response = requests.post(search_url, json=payload, headers=self.headers, timeout=30)
            response.raise_for_status()
            data = response.json()

            people = data.get("people", [])
            logger.info(f"Found {len(people)} people in free search", company=company_domain or company_name)
            return people

        except requests.RequestException as e:
            detail = self._extract_error_detail(e)
            logger.error("Apollo free people search failed", error=str(e), response_detail=detail)
            return []

    def find_contacts_free(
        self,
        company_domain: str = None,
        company_name: str = None,
        seniority: list[str] = None,
        limit: int = 1,
    ) -> list[dict]:
        """
        Find contacts using ONLY the free search endpoint (no credits).
        Returns name/title but NOT email addresses.
        Useful for phone-first pipelines that don't need emails.
        """
        people = self._search_people_free(
            company_domain=company_domain,
            company_name=company_name,
            seniority=seniority,
            limit=limit * 2,
        )

        contacts = []
        for person in people:
            if len(contacts) >= limit:
                break

            full_name = person.get("name") or ""
            if not full_name:
                first = person.get("first_name", "")
                last = person.get("last_name", "")
                full_name = f"{first} {last}".strip()

            if not full_name:
                continue

            contacts.append({
                "full_name": full_name,
                "title": person.get("title", ""),
                "linkedin_url": person.get("linkedin_url", ""),
            })

        logger.info("Found contacts (free)", company=company_domain or company_name, count=len(contacts))
        return contacts

    def find_contacts(
        self,
        company_domain: str = None,
        company_name: str = None,
        titles: list[str] = None,
        seniority: list[str] = None,
        limit: int = 3
    ) -> list[dict]:
        """
        Find contacts at a company using the free search, then enrich by ID
        to get emails (costs credits). Use find_contacts_free() if you don't
        need emails.
        """
        people = self._search_people_free(
            company_domain=company_domain,
            company_name=company_name,
            seniority=seniority,
            limit=limit * 2,
        )

        # Enrich people who have email (costs credits)
        contacts = []
        for person in people:
            if len(contacts) >= limit:
                break

            if not person.get("has_email"):
                continue

            person_id = person.get("id")
            if not person_id:
                continue

            enriched = self._enrich_person_by_id(person_id)
            if enriched and enriched.get("email"):
                contacts.append(enriched)

        logger.info(
            "Found contacts with emails",
            company=company_domain or company_name,
            count=len(contacts)
        )
        return contacts

    def _enrich_person_by_id(self, person_id: str) -> Optional[dict]:
        """
        Enrich a person by their Apollo ID to get full details including email.
        This costs credits.
        """
        if self._credits_exhausted or self._check_budget():
            return None

        url = f"{self.BASE_URL}/people/match"
        payload = {"id": person_id}

        try:
            response = requests.post(url, json=payload, headers=self.headers, timeout=30)
            response.raise_for_status()
            self._credits_used += 1
            person = response.json().get("person", {})

            if not person:
                return None

            return {
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
                "company_name": person.get("organization", {}).get("name") if person.get("organization") else None,
                "company_domain": person.get("organization", {}).get("primary_domain") if person.get("organization") else None
            }

        except requests.RequestException as e:
            self._check_credits_exhausted(e)
            detail = self._extract_error_detail(e)
            logger.error("Apollo person enrichment failed", error=str(e), person_id=person_id, response_detail=detail)
            return None

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
            self._check_credits_exhausted(e)
            detail = self._extract_error_detail(e)
            logger.error("Apollo email enrichment failed", error=str(e), response_detail=detail)
            return None


def enrich_lead(
    api_key: str,
    company_name: str,
    website: str = None,
    city: str = None,
    client: ApolloClient = None,
) -> dict:
    """
    Enrich a lead with Apollo data.

    Args:
        api_key: Apollo API key
        company_name: Company name
        website: Company website
        city: Company city
        client: Optional reusable ApolloClient (preserves credit-exhaustion state across calls)

    Returns:
        Enriched lead data
    """
    if client is None:
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

    # Find decision-maker contacts (limit=1: we only use the primary contact)
    contacts = client.find_contacts(
        company_domain=website,
        company_name=company_name,
        limit=1
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
