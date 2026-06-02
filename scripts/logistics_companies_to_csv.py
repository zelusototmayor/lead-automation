"""
logistics_companies_to_csv.py
=============================
One-shot company sourcer for the Logistics Quote Agent ICP.

Pulls freight forwarders / customs brokers in UK + Spain from Google Maps
and writes them to a CSV ready for direct import into Instantly. No Google
Sheets writes, no SuperSearch enrichment, no Instantly API calls —
Instantly's own enrichment step is expected to find the contact emails
after import.

Why this exists:
- Claude inside the Cowork sandbox cannot reach Google Maps / Instantly APIs
  from its environment (egress proxy blocks them).
- But running the full `src/logistics.py` pipeline locally wires in Sheets
  + SuperSearch too — heavier than needed when all we want is a company list.
- This script is the lean fallback: Google Maps → CSV → you import to Instantly.

Config:
- Reuses `config/settings.yaml` → `logistics.target_regions`,
  `logistics.search_queries`, `logistics.exclude_keywords`.
- Loads GOOGLE_MAPS_API_KEY from `.env`.

Usage (run from repo root):
    python scripts/logistics_companies_to_csv.py                       # default: 100 companies
    python scripts/logistics_companies_to_csv.py --target 150          # override count
    python scripts/logistics_companies_to_csv.py --max-per-query 8     # wider per-query pull
    python scripts/logistics_companies_to_csv.py --out mylist.csv      # custom path

Output columns (tuned for Instantly's "Import CSV" flow):
    company_name, website, domain, country, city, sub_segment,
    language, phone, address, rating, reviews_count, google_types
"""

import argparse
import csv
import os
import sys
import yaml
from pathlib import Path

# Make the repo's `src` importable regardless of where script is invoked from.
REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.lead_sourcing.google_maps import search_agencies  # noqa: E402


# Map each search query to a normalized sub_segment label for the CSV.
SUB_SEGMENT_BY_QUERY = {
    "freight forwarder": "Freight Forwarder",
    "customs broker":    "Customs Broker",
    "transitario":       "Freight Forwarder",
    "agencia de aduanas": "Customs Broker",
    "agente de aduanas":  "Customs Broker",
}


def _load_dotenv() -> None:
    env_file = REPO_ROOT / ".env"
    if not env_file.exists():
        return
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())


def _load_config() -> dict:
    with open(REPO_ROOT / "config/settings.yaml") as f:
        return yaml.safe_load(f)


def _extract_domain(website: str) -> str:
    if not website:
        return ""
    return (
        website.replace("https://", "")
               .replace("http://", "")
               .replace("www.", "")
               .split("/")[0]
               .strip()
               .lower()
    )


def _sub_segment_for_query(query: str) -> str:
    q = query.lower()
    for k, v in SUB_SEGMENT_BY_QUERY.items():
        if k in q:
            return v
    return "Logistics"


def _language_for_country(country: str) -> str:
    return "es" if country.upper() in ("ES", "SPAIN") else "en"


def main() -> int:
    parser = argparse.ArgumentParser(description="Build Logistics ICP CSV for Instantly import.")
    parser.add_argument("--target", type=int, default=100,
                        help="Target number of unique companies (default: 100)")
    parser.add_argument("--max-per-query", type=int, default=8,
                        help="Max Google Maps results per (city, query) pair")
    parser.add_argument("--out", type=str,
                        default=str(REPO_ROOT / "outputs" / "logistics_icp_companies.csv"),
                        help="Output CSV path")
    args = parser.parse_args()

    _load_dotenv()
    api_key = os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()
    if not api_key:
        print("ERROR: GOOGLE_MAPS_API_KEY not set in .env", file=sys.stderr)
        return 1

    config = _load_config()
    lc = config["logistics"]
    regions = lc["target_regions"]
    queries = lc["search_queries"]
    exclude_keywords = lc.get("exclude_keywords", [])

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"Target: {args.target} companies | regions: {len(regions)} | "
          f"queries: {len(queries)} | max_per_query: {args.max_per_query}")
    print(f"Writing to: {out_path}")

    seen_domains: set[str] = set()
    seen_names: set[str] = set()  # fallback dedup for entries with no website
    rows: list[dict] = []

    # Iterate regions in config order — UK first, Spain second, as listed.
    # Stop once we hit the target so we don't waste API calls.
    for region in regions:
        if len(rows) >= args.target:
            break

        city = region["name"]
        country = region["country"]
        lang = _language_for_country(country)

        print(f"  → {city}, {country}  (have {len(rows)} / {args.target})")

        try:
            firms = search_agencies(
                api_key=api_key,
                city=city,
                country=country,
                search_queries=queries,
                max_per_query=args.max_per_query,
                exclude_keywords=exclude_keywords,
            )
        except Exception as e:
            print(f"     search failed: {e}", file=sys.stderr)
            continue

        for firm in firms:
            if len(rows) >= args.target:
                break

            name = (firm.get("name") or "").strip()
            if not name:
                continue

            website = (firm.get("website") or "").strip()
            domain = _extract_domain(website)

            # Dedup: prefer domain; fall back to company name if no domain.
            if domain:
                if domain in seen_domains:
                    continue
                seen_domains.add(domain)
            else:
                key = name.lower()
                if key in seen_names:
                    continue
                seen_names.add(key)

            # Infer sub_segment from the place types or the fact that
            # we found it at all. Types don't usually tell us forwarder
            # vs. broker, so default to Freight Forwarder unless the
            # name makes it obvious.
            name_lower = name.lower()
            if "aduan" in name_lower or "customs" in name_lower:
                sub_segment = "Customs Broker"
            else:
                sub_segment = "Freight Forwarder"

            rows.append({
                "company_name": name,
                "website": website,
                "domain": domain,
                "country": country,
                "city": city,
                "sub_segment": sub_segment,
                "language": lang,
                "phone": firm.get("phone") or "",
                "address": firm.get("address") or "",
                "rating": firm.get("rating") or "",
                "reviews_count": firm.get("reviews_count") or "",
                "google_types": ",".join(firm.get("types") or []),
            })

    fieldnames = [
        "company_name", "website", "domain", "country", "city",
        "sub_segment", "language", "phone", "address",
        "rating", "reviews_count", "google_types",
    ]
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    # Summary
    uk_count = sum(1 for r in rows if r["country"].upper() == "UK")
    es_count = sum(1 for r in rows if r["country"].upper() == "ES")
    with_site = sum(1 for r in rows if r["website"])
    print()
    print(f"Done: {len(rows)} companies written")
    print(f"  UK: {uk_count}  |  ES: {es_count}")
    print(f"  With website: {with_site}  ({100*with_site/max(1,len(rows)):.0f}%)")
    print(f"  CSV: {out_path}")
    print()
    print("Next: Instantly → Leads → Upload CSV. Map 'company_name' → Company")
    print("and 'website' → Website, then run SuperSearch enrichment to add emails.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
