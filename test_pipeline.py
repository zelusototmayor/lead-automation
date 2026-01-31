"""
Test Pipeline - Run full flow but send to yourself
===================================================
This script finds one real lead, enriches it, personalizes it,
and adds it to Instantly with YOUR email so you receive the test.
"""

import os
import sys
import yaml
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent))

from src.lead_sourcing import search_agencies, enrich_lead
from src.outreach import EmailPersonalizer, InstantlyClient, calculate_lead_score


def load_config():
    """Load configuration."""
    config_path = Path(__file__).parent / "config" / "settings.yaml"
    with open(config_path, 'r') as f:
        config = yaml.safe_load(f)

    # Replace env vars
    def replace_env_vars(obj):
        if isinstance(obj, str) and obj.startswith("${") and obj.endswith("}"):
            env_var = obj[2:-1]
            return os.environ.get(env_var, "")
        elif isinstance(obj, dict):
            return {k: replace_env_vars(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [replace_env_vars(item) for item in obj]
        return obj

    return replace_env_vars(config)


def load_templates():
    """Load email templates."""
    template_path = Path(__file__).parent / "config" / "email_templates.yaml"
    with open(template_path, 'r') as f:
        return yaml.safe_load(f)


def test_pipeline(your_email: str, city: str = "Austin"):
    """
    Run the full pipeline for one lead, but send to your email.

    Args:
        your_email: Your email to receive the test
        city: City to search in
    """
    config = load_config()
    templates = load_templates()

    print("\n" + "="*60)
    print("🧪 TEST PIPELINE - Full flow with your email")
    print("="*60)

    # Step 1: Find agencies
    print(f"\n📍 Step 1: Searching for agencies in {city}...")
    agencies = search_agencies(
        api_key=config["api_keys"]["google_maps"],
        city=city,
        country="US",
        search_queries=["marketing agency"],
        max_per_query=3,
        exclude_keywords=config["lead_sourcing"]["exclude_keywords"]
    )

    if not agencies:
        print("❌ No agencies found. Try a different city.")
        return

    print(f"   Found {len(agencies)} agencies")
    for a in agencies[:3]:
        print(f"   - {a['name']}: {a.get('website', 'no website')}")

    # Pick first agency with a website
    agency = None
    for a in agencies:
        if a.get("website"):
            agency = a
            break

    if not agency:
        print("❌ No agency with website found.")
        return

    print(f"\n   Selected: {agency['name']}")

    # Step 2: Enrich with Apollo
    print(f"\n🔍 Step 2: Enriching with Apollo...")
    enriched = enrich_lead(
        api_key=config["api_keys"]["apollo"],
        company_name=agency["name"],
        website=agency.get("website"),
        city=city
    )

    primary_contact = enriched.get("primary_contact", {})
    print(f"   Company: {agency['name']}")
    print(f"   Industry: {enriched.get('industry', 'Unknown')}")
    print(f"   Employees: {enriched.get('employee_count', 'Unknown')}")
    print(f"   Contact: {primary_contact.get('full_name', 'Not found')}")
    print(f"   Title: {primary_contact.get('title', 'Unknown')}")
    print(f"   Email: {primary_contact.get('email', 'Not found')}")

    if not primary_contact.get("email"):
        print("\n⚠️  No email found from Apollo. Using placeholder for test.")
        primary_contact["email"] = "placeholder@example.com"
        primary_contact["full_name"] = primary_contact.get("full_name") or "Marketing Director"

    # Build lead data
    lead_data = {
        "company": agency["name"],
        "contact_name": primary_contact.get("full_name", ""),
        "email": primary_contact.get("email", ""),
        "phone": agency.get("phone", ""),
        "website": agency.get("website", ""),
        "industry": enriched.get("industry", "Marketing Agency"),
        "employee_count": enriched.get("employee_count", ""),
        "city": city,
        "country": "US",
        "linkedin": primary_contact.get("linkedin_url", ""),
        "title": primary_contact.get("title", ""),
        "description": enriched.get("description", ""),
        "technologies": enriched.get("technologies", []),
        "keywords": enriched.get("keywords", [])
    }

    lead_data["lead_score"] = calculate_lead_score(lead_data)
    print(f"   Lead Score: {lead_data['lead_score']}/10")

    # Step 3: Personalize with Claude
    print(f"\n✍️  Step 3: Personalizing email with Claude...")
    personalizer = EmailPersonalizer(
        api_key=config["api_keys"]["anthropic"],
        model=config["personalization"]["model"]
    )

    template = templates["sequences"]["default"]["emails"][0]["body_template"]
    sender_info = {
        "bio": config["personalization"]["sender_bio"],
        "value_proposition": config["personalization"]["value_proposition"]
    }

    personalized = personalizer.personalize_email(
        lead=lead_data,
        template=template,
        sender_info=sender_info
    )

    print(f"\n   --- Personalized Content ---")
    print(f"   Opener: {personalized.get('personalized_opener', 'N/A')}")
    print(f"   Pain Point: {personalized.get('specific_pain_point', 'N/A')}")
    print(f"   Industry Insight: {personalized.get('industry_specific_insight', 'N/A')}")

    # Add personalization to lead
    lead_data.update({
        "personalized_opener": personalized.get("personalized_opener", ""),
        "specific_pain_point": personalized.get("specific_pain_point", ""),
        "industry_specific_insight": personalized.get("industry_specific_insight", ""),
        "first_name": lead_data["contact_name"].split()[0] if lead_data["contact_name"] else "there"
    })

    # Step 4: Add to Instantly (with YOUR email)
    print(f"\n📧 Step 4: Adding to Instantly campaign...")
    print(f"   ⚠️  Replacing email with: {your_email}")

    # Override the email with yours for testing
    lead_data["email"] = your_email

    instantly = InstantlyClient(config.get("instantly", {}).get("api_key", ""))

    campaign_name = config.get("instantly", {}).get("campaign_name", "Agency Outreach")
    campaigns = instantly.list_campaigns()

    campaign_id = None
    for campaign in campaigns:
        if campaign.get("name") == campaign_name:
            campaign_id = campaign.get("id")
            print(f"   Found campaign: {campaign_name} (ID: {campaign_id})")
            break

    if not campaign_id:
        print(f"   ❌ Campaign '{campaign_name}' not found in Instantly!")
        print(f"   Available campaigns: {[c.get('name') for c in campaigns]}")
        return

    result = instantly.add_leads_to_campaign(campaign_id, [lead_data])

    if result:
        print(f"   ✅ Lead added to campaign!")
        print(f"\n" + "="*60)
        print("🎉 TEST COMPLETE!")
        print("="*60)
        print(f"\nYou should receive an email at {your_email}")
        print(f"(depending on your Instantly campaign schedule)")
        print(f"\nThe email will be personalized for: {agency['name']}")
        print(f"Contact name used: {lead_data['contact_name']}")
    else:
        print(f"   ❌ Failed to add lead to campaign")


if __name__ == "__main__":
    # Your email to receive the test
    YOUR_EMAIL = "zelu@zelusottomayor.com"

    test_pipeline(your_email=YOUR_EMAIL, city="Austin")
