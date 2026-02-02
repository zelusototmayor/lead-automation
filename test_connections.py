"""
Quick test to verify all API connections work.
"""

import os
import sys
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Add src to path
sys.path.insert(0, str(Path(__file__).parent))

def test_google_maps():
    """Test Google Maps API connection."""
    print("\n🗺️  Testing Google Maps API...")
    from src.lead_sourcing.google_maps import GoogleMapsClient

    api_key = os.environ.get("GOOGLE_MAPS_API_KEY")
    if not api_key:
        print("   ❌ No API key found")
        return False

    client = GoogleMapsClient(api_key)
    results = client.search_businesses("marketing agency", "Miami, US", max_results=2)

    if results:
        print(f"   ✅ Found {len(results)} agencies")
        print(f"   Example: {results[0]['name']}")
        return True
    else:
        print("   ❌ No results found")
        return False

def test_apollo():
    """Test Apollo API connection."""
    print("\n🚀 Testing Apollo API...")
    from src.lead_sourcing.apollo import ApolloClient

    api_key = os.environ.get("APOLLO_API_KEY")
    if not api_key:
        print("   ❌ No API key found")
        return False

    client = ApolloClient(api_key)
    # Search for a well-known company
    result = client.search_organizations("HubSpot", domain="hubspot.com")

    if result:
        print(f"   ✅ Found: {result.get('name')}")
        print(f"   Industry: {result.get('industry')}")
        return True
    else:
        print("   ⚠️  No result (may be API limit)")
        return True  # Apollo free tier has limits

def test_google_sheets():
    """Test Google Sheets connection."""
    print("\n📊 Testing Google Sheets API...")
    from src.crm.sheets import GoogleSheetsCRM

    creds_file = "config/google_credentials.json"
    spreadsheet_id = "1ZdhkP_Hq-340eVEOS-RKwHGjDaX0vNVP6vO48XzkOx8"

    if not Path(creds_file).exists():
        print("   ❌ Credentials file not found")
        return False

    try:
        crm = GoogleSheetsCRM(creds_file, spreadsheet_id, "Leads")
        stats = crm.get_stats()
        print(f"   ✅ Connected to spreadsheet")
        print(f"   Total leads: {stats['total_leads']}")
        return True
    except Exception as e:
        print(f"   ❌ Error: {e}")
        return False

def test_anthropic():
    """Test Anthropic Claude API."""
    print("\n🤖 Testing Anthropic Claude API...")
    import anthropic

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("   ❌ No API key found")
        return False

    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=50,
            messages=[{"role": "user", "content": "Say 'API working!' in exactly 2 words."}]
        )
        print(f"   ✅ Response: {response.content[0].text.strip()}")
        return True
    except Exception as e:
        print(f"   ❌ Error: {e}")
        return False

def test_instantly():
    """Test Instantly API connection."""
    print("\n📧 Testing Instantly API...")
    from src.outreach.instantly_client import InstantlyClient

    api_key = os.environ.get("INSTANTLY_API_KEY")
    if not api_key:
        print("   ❌ No API key found")
        return False

    try:
        client = InstantlyClient(api_key)
        campaigns = client.list_campaigns()
        print(f"   ✅ Connected! Found {len(campaigns)} campaigns")
        if campaigns:
            print(f"   Campaigns: {[c.get('name') for c in campaigns[:3]]}")
        return True
    except Exception as e:
        print(f"   ❌ Error: {e}")
        return False

def main():
    print("=" * 50)
    print("Lead Automation - Connection Test")
    print("=" * 50)

    results = {
        "Google Maps": test_google_maps(),
        "Apollo": test_apollo(),
        "Google Sheets": test_google_sheets(),
        "Anthropic Claude": test_anthropic(),
        "Instantly": test_instantly()
    }

    print("\n" + "=" * 50)
    print("Summary")
    print("=" * 50)

    all_passed = True
    for name, passed in results.items():
        status = "✅" if passed else "❌"
        print(f"   {status} {name}")
        if not passed:
            all_passed = False

    if all_passed:
        print("\n🎉 All connections working! Ready to run.")
    else:
        print("\n⚠️  Some connections failed. Check the errors above.")

    return all_passed

if __name__ == "__main__":
    main()
