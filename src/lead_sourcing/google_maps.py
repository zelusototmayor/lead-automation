"""
Google Maps Places API - Lead Sourcing Module
==============================================
Finds agencies using Google Maps Places API.
"""

import requests
import time
from typing import Optional
import structlog

logger = structlog.get_logger()


class GoogleMapsClient:
    """Client for Google Maps Places API."""

    BASE_URL = "https://maps.googleapis.com/maps/api/place"

    def __init__(self, api_key: str):
        self.api_key = api_key

    def search_businesses(
        self,
        query: str,
        location: str,
        radius_meters: int = 50000,  # 50km radius
        max_results: int = 20
    ) -> list[dict]:
        """
        Search for businesses using text search.

        Args:
            query: Search query (e.g., "marketing agency")
            location: City name or coordinates
            radius_meters: Search radius in meters
            max_results: Maximum number of results to return

        Returns:
            List of business dictionaries
        """
        # First, geocode the location to get coordinates
        coords = self._geocode_location(location)
        if not coords:
            logger.warning("Could not geocode location", location=location)
            return []

        # Perform text search
        url = f"{self.BASE_URL}/textsearch/json"
        params = {
            "query": query,
            "location": f"{coords['lat']},{coords['lng']}",
            "radius": radius_meters,
            "key": self.api_key,
            "type": "establishment"
        }

        results = []
        next_page_token = None

        while len(results) < max_results:
            if next_page_token:
                params["pagetoken"] = next_page_token
                # Google requires a short delay before using page tokens
                time.sleep(2)

            try:
                response = requests.get(url, params=params, timeout=30)
                response.raise_for_status()
                data = response.json()

                if data.get("status") != "OK":
                    if data.get("status") == "ZERO_RESULTS":
                        logger.info("No results found", query=query, location=location)
                        break
                    logger.error(
                        "Google Maps API error",
                        status=data.get("status"),
                        error=data.get("error_message")
                    )
                    break

                for place in data.get("results", []):
                    business = self._parse_place(place)
                    if business:
                        results.append(business)

                    if len(results) >= max_results:
                        break

                next_page_token = data.get("next_page_token")
                if not next_page_token:
                    break

            except requests.RequestException as e:
                logger.error("Request failed", error=str(e))
                break

        logger.info(
            "Search completed",
            query=query,
            location=location,
            results_count=len(results)
        )
        return results

    def get_place_details(self, place_id: str) -> Optional[dict]:
        """
        Get detailed information about a place.

        Args:
            place_id: Google Place ID

        Returns:
            Dictionary with place details or None
        """
        url = f"{self.BASE_URL}/details/json"
        params = {
            "place_id": place_id,
            "fields": "name,formatted_address,formatted_phone_number,website,rating,user_ratings_total,types,business_status,opening_hours",
            "key": self.api_key
        }

        try:
            response = requests.get(url, params=params, timeout=30)
            response.raise_for_status()
            data = response.json()

            if data.get("status") != "OK":
                logger.warning(
                    "Could not get place details",
                    place_id=place_id,
                    status=data.get("status")
                )
                return None

            result = data.get("result", {})
            return {
                "name": result.get("name"),
                "address": result.get("formatted_address"),
                "phone": result.get("formatted_phone_number"),
                "website": result.get("website"),
                "rating": result.get("rating"),
                "reviews_count": result.get("user_ratings_total"),
                "types": result.get("types", []),
                "business_status": result.get("business_status"),
                "is_open": result.get("opening_hours", {}).get("open_now")
            }

        except requests.RequestException as e:
            logger.error("Failed to get place details", error=str(e))
            return None

    def _geocode_location(self, location: str) -> Optional[dict]:
        """Convert location name to coordinates."""
        url = "https://maps.googleapis.com/maps/api/geocode/json"
        params = {
            "address": location,
            "key": self.api_key
        }

        try:
            response = requests.get(url, params=params, timeout=30)
            response.raise_for_status()
            data = response.json()

            if data.get("status") == "OK" and data.get("results"):
                location_data = data["results"][0]["geometry"]["location"]
                return {
                    "lat": location_data["lat"],
                    "lng": location_data["lng"]
                }
        except requests.RequestException as e:
            logger.error("Geocoding failed", error=str(e))

        return None

    def _parse_place(self, place: dict) -> Optional[dict]:
        """Parse a place result into a standardized format."""
        if not place.get("name"):
            return None

        return {
            "place_id": place.get("place_id"),
            "name": place.get("name"),
            "address": place.get("formatted_address"),
            "rating": place.get("rating"),
            "reviews_count": place.get("user_ratings_total", 0),
            "types": place.get("types", []),
            "business_status": place.get("business_status", "OPERATIONAL"),
            "source": "google_maps"
        }


def search_agencies(
    api_key: str,
    city: str,
    country: str,
    search_queries: list[str],
    max_per_query: int = 5,
    exclude_keywords: list[str] = None
) -> list[dict]:
    """
    Search for agencies in a city using multiple queries.

    Args:
        api_key: Google Maps API key
        city: City name
        country: Country code
        search_queries: List of search queries
        max_per_query: Maximum results per query
        exclude_keywords: Keywords to exclude from results

    Returns:
        List of unique agencies found
    """
    client = GoogleMapsClient(api_key)
    location = f"{city}, {country}"
    exclude_keywords = exclude_keywords or []

    all_results = []
    seen_place_ids = set()

    for query in search_queries:
        full_query = f"{query} in {city}"
        results = client.search_businesses(
            query=full_query,
            location=location,
            max_results=max_per_query
        )

        for result in results:
            # Skip duplicates
            if result["place_id"] in seen_place_ids:
                continue

            # Skip excluded keywords
            name_lower = result["name"].lower()
            if any(kw.lower() in name_lower for kw in exclude_keywords):
                logger.debug("Excluded by keyword", name=result["name"])
                continue

            # Skip non-operational businesses
            if result.get("business_status") != "OPERATIONAL":
                continue

            seen_place_ids.add(result["place_id"])

            # Get additional details
            details = client.get_place_details(result["place_id"])
            if details:
                result.update(details)

            result["city"] = city
            result["country"] = country

            all_results.append(result)

            # Small delay to respect rate limits
            time.sleep(0.5)

    logger.info(
        "Agency search completed",
        city=city,
        total_found=len(all_results)
    )
    return all_results
