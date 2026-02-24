"""
Send a single dummy lead to Instantly for field-mapping verification.
"""

import os
import sys
import yaml
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.outreach import InstantlyClient


def load_config():
    config_path = Path(__file__).parent.parent / "config" / "settings.yaml"
    with open(config_path, "r") as f:
        config = yaml.safe_load(f)

    def replace_env_vars(obj):
        if isinstance(obj, str) and obj.startswith("${") and obj.endswith("}"):
            env_var = obj[2:-1]
            return os.environ.get(env_var, "")
        if isinstance(obj, dict):
            return {k: replace_env_vars(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [replace_env_vars(item) for item in obj]
        return obj

    return replace_env_vars(config)


def main():
    config = load_config()
    api_key = config.get("instantly", {}).get("api_key", "")
    if not api_key:
        print("INSTANTLY_API_KEY not set. Aborting.")
        return

    instantly = InstantlyClient(api_key)

    campaign_name = config.get("instantly", {}).get("campaign_name", "Agency Outreach")
    campaigns = instantly.list_campaigns()
    campaign_id = None
    for campaign in campaigns:
        if campaign.get("name") == campaign_name:
            campaign_id = campaign.get("id")
            break

    if not campaign_id:
        print(f"Campaign '{campaign_name}' not found. Available: {[c.get('name') for c in campaigns]}")
        return

    # Dummy lead with all fields populated
    lead = {
        "email": "zsottomayor@gmail.com",
        "first_name": "Zelu",
        "last_name": "Test",
        "company": "Dummy Co LLC",
        "website": "https://dummyco.example",
        "phone": "+1-555-0100",
        "industry": "Marketing Agency",
        "city": "Austin",
        "personalized_opener": "Saw Dummy Co's recent case study—solid positioning for mid‑market teams.",
        "specific_pain_point": "Teams like yours often lose leads from inconsistent follow‑ups across channels.",
        "industry_specific_insight": "Agencies winning outbound right now automate enrichment + first‑touch within minutes."
    }

    result = instantly.add_leads_to_campaign(campaign_id, [lead])
    if result:
        print("Dummy lead added. Check Instantly for field values.")
    else:
        print("Failed to add dummy lead.")


if __name__ == "__main__":
    main()
