"""
Apify Client — LinkedIn Jobs Scraper for Hiring Signals
=========================================================
Finds B2B companies hiring SDR/BDR roles via LinkedIn job postings.
Uses the HarvestAPI LinkedIn Job Search actor (no cookies/login needed).

Actor input: jobTitles[], locations[], maxItems, postedLimit, sortBy
Actor output: id, title, linkedinUrl, headerCaptionText, descriptionText,
              location{parsed{country, city, countryCode}}, company{}, ...
"""

import time
import requests
from typing import Optional
import structlog

logger = structlog.get_logger()

# HarvestAPI LinkedIn Job Search — $1/1,000 jobs, no login required
LINKEDIN_JOBS_ACTOR = "harvestapi~linkedin-job-search"


class ApifyClient:
    """Client for Apify actors with budget tracking."""

    BASE_URL = "https://api.apify.com/v2"

    def __init__(self, api_key: str, max_runs_per_session: int = 20):
        self.api_key = api_key
        self.max_runs = max_runs_per_session
        self._runs_used = 0
        self.headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }

    @property
    def runs_used(self) -> int:
        return self._runs_used

    def _run_actor(
        self,
        actor_id: str,
        input_data: dict,
        timeout_secs: int = 300,
        poll_interval: int = 10,
    ) -> Optional[list[dict]]:
        """Start an actor run, wait for completion, return dataset items."""
        if self._runs_used >= self.max_runs:
            logger.warning("Apify run budget exhausted",
                           used=self._runs_used, max=self.max_runs)
            return None

        url = f"{self.BASE_URL}/acts/{actor_id}/runs"
        try:
            resp = requests.post(
                url, json=input_data, headers=self.headers, timeout=30
            )
            resp.raise_for_status()
            run_data = resp.json().get("data", {})
            run_id = run_data.get("id")
            dataset_id = run_data.get("defaultDatasetId")
            self._runs_used += 1
            logger.info("Apify actor started",
                        actor=actor_id, run_id=run_id)
        except requests.RequestException as e:
            logger.error("Failed to start Apify actor",
                         actor=actor_id, error=str(e))
            return None

        # Poll for completion
        status_url = f"{self.BASE_URL}/actor-runs/{run_id}"
        elapsed = 0
        while elapsed < timeout_secs:
            time.sleep(poll_interval)
            elapsed += poll_interval
            try:
                resp = requests.get(status_url, headers=self.headers, timeout=15)
                resp.raise_for_status()
                status = resp.json().get("data", {}).get("status")
                if status == "SUCCEEDED":
                    break
                elif status in ("FAILED", "ABORTED", "TIMED-OUT"):
                    logger.error("Apify actor run failed",
                                 run_id=run_id, status=status)
                    return None
            except requests.RequestException:
                pass
        else:
            logger.error("Apify actor run timed out",
                         run_id=run_id, timeout=timeout_secs)
            return None

        # Fetch dataset items
        items_url = f"{self.BASE_URL}/datasets/{dataset_id}/items"
        try:
            resp = requests.get(
                items_url,
                headers=self.headers,
                params={"format": "json", "limit": 2000},
                timeout=30,
            )
            resp.raise_for_status()
            items = resp.json()
            logger.info("Apify actor results fetched",
                        actor=actor_id, items=len(items))
            return items
        except requests.RequestException as e:
            logger.error("Failed to fetch Apify dataset",
                         dataset_id=dataset_id, error=str(e))
            return None

    def search_linkedin_jobs(
        self,
        job_titles: list[str],
        locations: list[str],
        max_items: int = 100,
        posted_limit: str = "month",
    ) -> list[dict]:
        """Search LinkedIn jobs using the HarvestAPI actor.

        Runs a single actor call with all job titles x locations.
        """
        input_data = {
            "jobTitles": job_titles,
            "locations": locations,
            "maxItems": max_items,
            "postedLimit": posted_limit,
            "sortBy": "date",
        }
        items = self._run_actor(
            LINKEDIN_JOBS_ACTOR,
            input_data,
            timeout_secs=900,
        )
        return items or []


def _extract_company_name(job: dict) -> str:
    """Extract company name from a LinkedIn job result."""
    # Try dedicated fields first
    for field in ("companyName", "company_name"):
        val = job.get(field)
        if val and isinstance(val, str):
            return val.strip()

    # company object might have a name
    company_obj = job.get("company")
    if isinstance(company_obj, dict) and company_obj.get("name"):
        return company_obj["name"].strip()

    # Fall back to first line of headerCaptionText
    header = job.get("headerCaptionText", "")
    if header and isinstance(header, str):
        first_line = header.split("\n")[0].strip()
        if first_line and not first_line.startswith("http"):
            return first_line

    return ""


def _extract_location_info(job: dict) -> dict:
    """Extract structured location from a LinkedIn job result."""
    loc = job.get("location", {})
    if isinstance(loc, dict):
        parsed = loc.get("parsed", {})
        return {
            "country": parsed.get("country") or parsed.get("countryFull") or "",
            "city": parsed.get("city") or "",
            "state": parsed.get("state") or "",
            "country_code": parsed.get("countryCode") or loc.get("countryCode") or "",
            "location_text": loc.get("linkedinText") or parsed.get("text") or "",
        }

    # If location is a string
    if isinstance(loc, str):
        return {"country": "", "city": "", "state": "", "country_code": "",
                "location_text": loc}

    # From headerCaptionText line 2
    header = job.get("headerCaptionText", "")
    if header:
        lines = header.split("\n")
        if len(lines) >= 2:
            return {"country": "", "city": "", "state": "", "country_code": "",
                    "location_text": lines[1].strip()}

    return {"country": "", "city": "", "state": "", "country_code": "",
            "location_text": ""}


def search_linkedin_hiring_signals(
    client: ApifyClient,
    queries: list[str],
    locations: list[str],
    exclude_companies: list[str] = None,
    max_results_per_search: int = 50,
) -> list[dict]:
    """Search LinkedIn for companies hiring SDR/BDR roles.

    Returns rich signal dicts with all available job data (no Apollo needed).
    """
    exclude = set(c.lower() for c in (exclude_companies or []))
    seen_companies: dict[str, dict] = {}

    jobs = client.search_linkedin_jobs(
        job_titles=queries,
        locations=locations,
        max_items=max_results_per_search,
        posted_limit="month",
    )

    for job in jobs:
        company = _extract_company_name(job)
        if not company:
            continue

        company_lower = company.lower()

        if any(exc in company_lower for exc in exclude):
            continue

        if company_lower in seen_companies:
            continue

        loc_info = _extract_location_info(job)

        seen_companies[company_lower] = {
            "company_name": company,
            "signal_type": "linkedin_hiring",
            "signal_detail": f"Hiring: {job.get('title', '')}",
            "job_title": job.get("title", ""),
            "job_url": job.get("linkedinUrl", ""),
            "job_posted_date": job.get("postedDate", ""),
            "employment_type": job.get("employmentType", ""),
            "workplace_type": job.get("workplaceType", ""),
            # Location
            "country": loc_info["country"],
            "city": loc_info["city"],
            "country_code": loc_info["country_code"],
            "location_text": loc_info["location_text"],
            # Company (from LinkedIn — might be sparse)
            "company_linkedin_url": job.get("companyUrl", ""),
            # Full job description for B2B filtering
            "description_text": job.get("descriptionText", "") or "",
        }

    signals = list(seen_companies.values())
    logger.info("LinkedIn hiring signals collected",
                total_jobs=len(jobs),
                unique_companies=len(signals),
                apify_runs_used=client.runs_used)
    return signals
