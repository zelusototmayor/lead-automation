"""
SerpAPI Client — Signal-Based Lead Sourcing
=============================================
Finds B2B startups via hiring signals (job postings) and funding signals (news).
Uses SerpAPI's Google Jobs and Google Search engines.
"""

import requests
from typing import Optional
import structlog

logger = structlog.get_logger()


class SerpAPIClient:
    """Client for SerpAPI with budget tracking."""

    BASE_URL = "https://serpapi.com/search"

    def __init__(self, api_key: str, budget_per_run: int = 30):
        """
        Args:
            api_key: SerpAPI API key
            budget_per_run: Max searches per run (250/month total)
        """
        self.api_key = api_key
        self.budget_per_run = budget_per_run
        self._searches_used = 0

    @property
    def searches_used(self) -> int:
        return self._searches_used

    @property
    def budget_remaining(self) -> int:
        return max(0, self.budget_per_run - self._searches_used)

    def _search(self, params: dict) -> Optional[dict]:
        """Execute a SerpAPI search with budget tracking."""
        if self._searches_used >= self.budget_per_run:
            logger.warning("SerpAPI budget exhausted for this run",
                           used=self._searches_used, budget=self.budget_per_run)
            return None

        params["api_key"] = self.api_key
        params["no_cache"] = "false"

        try:
            response = requests.get(self.BASE_URL, params=params, timeout=30)
            response.raise_for_status()
            self._searches_used += 1
            return response.json()
        except requests.RequestException as e:
            logger.error("SerpAPI request failed", error=str(e), params={k: v for k, v in params.items() if k != "api_key"})
            return None

    def search_jobs(self, query: str, location: str = "United States") -> list[dict]:
        """Search Google Jobs via SerpAPI.

        Returns list of job results with company_name, title, location, etc.
        """
        result = self._search({
            "engine": "google_jobs",
            "q": query,
            "location": location,
            "hl": "en",
        })

        if not result:
            return []

        jobs = result.get("jobs_results", [])
        logger.info("SerpAPI jobs search", query=query, location=location, results=len(jobs))
        return jobs

    def search_google(self, query: str, num: int = 10) -> list[dict]:
        """Search Google via SerpAPI.

        Returns list of organic results with title, snippet, link, etc.
        """
        result = self._search({
            "engine": "google",
            "q": query,
            "num": num,
            "hl": "en",
            "gl": "us",
        })

        if not result:
            return []

        results = result.get("organic_results", [])
        logger.info("SerpAPI google search", query=query, results=len(results))
        return results


def search_hiring_signals(
    api_key: str,
    queries: list[str],
    locations: list[str],
    exclude_companies: list[str] = None,
    budget_per_run: int = 30,
    client: SerpAPIClient = None,
) -> list[dict]:
    """
    Search for companies hiring SDRs/BDRs via Google Jobs.

    Args:
        api_key: SerpAPI API key
        queries: Job title queries (e.g., ["SDR", "BDR"])
        locations: Locations to search (e.g., ["United States", "New York"])
        exclude_companies: Large companies to skip
        budget_per_run: Max searches per run
        client: Optional reusable SerpAPIClient

    Returns:
        List of signal dicts: {company_name, signal_type, signal_detail, location, ...}
    """
    if client is None:
        client = SerpAPIClient(api_key, budget_per_run=budget_per_run)

    exclude = set(c.lower() for c in (exclude_companies or []))
    seen_companies = set()
    signals = []

    for query in queries:
        for location in locations:
            if client.budget_remaining <= 0:
                break

            jobs = client.search_jobs(query=query, location=location)

            for job in jobs:
                company = job.get("company_name", "").strip()
                if not company:
                    continue

                company_lower = company.lower()

                # Skip excluded companies
                if any(exc in company_lower for exc in exclude):
                    continue

                # Deduplicate by company name
                if company_lower in seen_companies:
                    continue
                seen_companies.add(company_lower)

                signals.append({
                    "company_name": company,
                    "signal_type": "hiring_signal",
                    "signal_detail": f"Hiring: {job.get('title', query)}",
                    "location": job.get("location", location),
                    "source_query": query,
                    "job_title": job.get("title", ""),
                    "description_snippet": (job.get("description", "") or "")[:200],
                })

    logger.info("Hiring signals collected",
                total=len(signals), searches_used=client.searches_used)
    return signals


def search_funding_signals(
    api_key: str,
    queries: list[str],
    exclude_companies: list[str] = None,
    budget_per_run: int = 30,
    client: SerpAPIClient = None,
) -> list[dict]:
    """
    Search for recently funded startups via Google News/Search.

    Args:
        api_key: SerpAPI API key
        queries: Funding search queries
        exclude_companies: Large companies to skip
        budget_per_run: Max searches per run
        client: Optional reusable SerpAPIClient

    Returns:
        List of signal dicts: {company_name, signal_type, signal_detail, ...}
    """
    if client is None:
        client = SerpAPIClient(api_key, budget_per_run=budget_per_run)

    exclude = set(c.lower() for c in (exclude_companies or []))
    seen_companies = set()
    signals = []

    for query in queries:
        if client.budget_remaining <= 0:
            break

        results = client.search_google(query=query, num=10)

        for result in results:
            title = result.get("title", "")
            snippet = result.get("snippet", "")
            text = f"{title} {snippet}"

            # Try to extract company name from the title
            # Common patterns: "CompanyName raises $Xm..." or "CompanyName announces..."
            company = _extract_company_from_funding_text(title)
            if not company:
                continue

            company_lower = company.lower()

            if any(exc in company_lower for exc in exclude):
                continue

            if company_lower in seen_companies:
                continue
            seen_companies.add(company_lower)

            # Determine funding type from text
            signal_detail = _extract_funding_detail(text)

            signals.append({
                "company_name": company,
                "signal_type": "funding_signal",
                "signal_detail": signal_detail,
                "source_query": query,
                "source_url": result.get("link", ""),
                "description_snippet": snippet[:200],
            })

    logger.info("Funding signals collected",
                total=len(signals), searches_used=client.searches_used)
    return signals


def _extract_company_from_funding_text(title: str) -> str:
    """Extract company name from a funding news headline.

    Handles patterns like:
    - "Acme Corp Raises $5M Series A"
    - "Acme Corp announces $10M seed round"
    - "Acme Corp closes $3M funding round"
    """
    if not title:
        return ""

    # Split on common funding verbs
    for verb in ["raises", "raised", "announces", "announced", "closes",
                 "closed", "secures", "secured", "lands", "nabs", "gets",
                 "receives", "received", "completes"]:
        lower = title.lower()
        idx = lower.find(f" {verb} ")
        if idx > 0:
            return title[:idx].strip().strip('"').strip("'")

    return ""


def _extract_funding_detail(text: str) -> str:
    """Extract funding round details from text."""
    text_lower = text.lower()

    if "series b" in text_lower:
        return "Raised Series B"
    elif "series a" in text_lower:
        return "Raised Series A"
    elif "seed" in text_lower:
        return "Raised Seed Round"
    elif "pre-seed" in text_lower:
        return "Raised Pre-Seed"
    elif "series c" in text_lower:
        return "Raised Series C"
    else:
        return "Recently Funded"
