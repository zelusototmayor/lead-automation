"""Wait long for work-email enrichment. Also look for an explicit enrich endpoint."""
import os
import sys
import json
import time
import requests
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

env_file = Path(__file__).parent.parent / ".env"
with open(env_file) as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

api_key = os.environ["INSTANTLY_API_KEY"]
headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
BASE = "https://api.instantly.ai/api/v2"

# 1. Kickoff + long poll
print("=== Long poll: does email ever arrive? ===")
payload = {
    "search_filters": {
        "company_name": {"include": ["FedEx"]},
        "locations": [{"country": "GB"}],
        "seniority": ["director", "vp", "c_suite"],
    },
    "limit": 2,
    "work_email_enrichment": True,
}
r = requests.post(f"{BASE}/supersearch-enrichment/enrich-leads-from-supersearch",
                  headers=headers, json=payload, timeout=60)
data = r.json()
list_id = data.get("resource_id")
task_id = data.get("id")
print(f"list_id={list_id}, task_id={task_id}")

lead_id = None
for t in [5, 15, 30, 60, 120, 180, 240]:
    time.sleep(t if t == 5 else t - [5, 15, 30, 60, 120, 180][[5, 15, 30, 60, 120, 180, 240].index(t) - 1])
    r = requests.post(f"{BASE}/leads/list", headers=headers,
                      json={"list_id": list_id, "limit": 5}, timeout=30)
    items = r.json().get("items", []) if r.status_code == 200 else []
    with_email = [it for it in items if it.get("email")]
    print(f"[t={t}s] items={len(items)} with_email={len(with_email)}")
    for it in items:
        print(f"    {it.get('first_name','')} {it.get('last_name','')} @ {it.get('company_name','')} "
              f"email={it.get('email','')!r} status={it.get('status')} "
              f"esp_code={it.get('esp_code')}")
        if not lead_id:
            lead_id = it.get("id")
    if items and all(it.get("email") for it in items):
        break

# 2. Look for explicit enrichment endpoint for a single lead
print("\n=== Try explicit lead enrichment endpoints ===")
if lead_id:
    for url in [
        f"{BASE}/leads/enrich",
        f"{BASE}/leads/{lead_id}/enrich",
        f"{BASE}/enrichment/enrich-lead",
        f"{BASE}/email-finder",
        f"{BASE}/supersearch-enrichment/enrich-email",
    ]:
        for method in ["POST", "GET"]:
            kwargs = {"headers": headers, "timeout": 15}
            if method == "POST":
                kwargs["json"] = {"lead_id": lead_id}
            try:
                r = requests.request(method, url, **kwargs)
                print(f"  {method} {url} -> {r.status_code} "
                      f"body={r.text[:150]!r}")
            except Exception as e:
                print(f"  {method} {url} -> EXC {e}")

# 3. Check Instantly credit / account info
print("\n=== Account / billing info ===")
for url in [f"{BASE}/accounts", f"{BASE}/account", f"{BASE}/workspaces",
            f"{BASE}/api-keys", f"{BASE}/usage"]:
    try:
        r = requests.get(url, headers=headers, timeout=15)
        print(f"  GET {url} -> {r.status_code} body={r.text[:200]!r}")
    except Exception as e:
        print(f"  GET {url} -> EXC {e}")
