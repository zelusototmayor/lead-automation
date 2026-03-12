# Lead Automation System

Multi-campaign outbound system: signal-based lead sourcing → Apollo enrichment → Claude personalization → Instantly email sequences. Managed via Google Sheets CRM + FastAPI dashboard.

## Campaigns

| Campaign | ICP | Sourcing | Outreach | Daily Target |
|---|---|---|---|---|
| **AEC** | Architecture/Engineering firms, 5-200 employees | Google Maps | Email (4-step Instantly) | 50 leads |
| **Local Services** | Staffing, logistics, real estate, insurance | Google Maps | Phone (dashboard CRM) | 50 leads |
| **B2B Startups** | B2B SaaS, 5-50 employees, hiring sales roles | Apollo + SerpAPI | Email (4-step Instantly) | 25 leads |

See [`docs/CAMPAIGNS.md`](docs/CAMPAIGNS.md) for full ICP definitions, signal logic, and email strategies per campaign.

## Project Structure

```
lead-automation/
├── src/
│   ├── main.py                    # AEC pipeline runner
│   ├── local_services.py          # Local Services pipeline runner
│   ├── startups.py                # B2B Startups pipeline runner
│   ├── lead_sourcing/
│   │   ├── apollo.py              # Apollo.io: org/people search + email enrichment
│   │   ├── google_maps.py         # Google Maps: location-based business discovery
│   │   └── serpapi.py             # SerpAPI: Google Jobs/hiring signals
│   ├── crm/
│   │   ├── sheets.py              # CRM for email-first pipelines (AEC, Startups)
│   │   └── local_services_sheet.py # CRM for phone-first pipeline
│   └── outreach/
│       ├── personalize.py         # Claude AI email personalization + lead scoring
│       ├── instantly_client.py    # Instantly.ai V2 API client
│       └── sync_instantly.py      # Sync Instantly engagement → Google Sheets
├── dashboard/app/
│   ├── main.py                    # FastAPI dashboard (metrics + cold calling CRM)
│   ├── auth.py                    # Simple auth
│   └── metrics.py                 # CRM metric calculations
├── config/
│   ├── settings.yaml              # All pipeline configs (env-templated API keys)
│   └── email_templates.yaml       # AEC 4-email sequence
├── scripts/
│   ├── deploy-automation.sh       # Build amd64 image → push to Docker Hub → pull on server
│   └── run-startups.sh            # Server cron wrapper for B2B Startups pipeline
└── docs/
    ├── CAMPAIGNS.md               # Per-campaign ICP, sourcing logic, outreach strategy
    ├── ARCHITECTURE.md            # Data flows, module map, API cost reference
    └── DEPLOY.md                  # Server setup, deploy workflow, cron schedule
```

## Quick Start (Local)

```bash
# Install
pip install -r requirements.txt

# Configure
cp .env.example .env  # fill in API keys
# Place google_credentials.json in config/

# Run AEC pipeline
python src/main.py

# Run B2B Startups pipeline
python src/startups.py --target 25

# Run Local Services pipeline
python src/local_services.py --target 50
```

## Configuration

All settings live in `config/settings.yaml`. API keys are env-templated (`${VARIABLE_NAME}`) and loaded from `.env`.

Key settings per campaign:
- `lead_sourcing.daily_target` — AEC daily target
- `lead_sourcing.apollo_credit_budget` — Apollo credits per AEC run
- `startups.daily_target` / `startups.apollo_credit_budget` — Startups budget
- `local_services.daily_target` — Local Services target

## Deploy

```bash
./scripts/deploy-automation.sh          # build + push + pull on server
./scripts/deploy-automation.sh --sync-config  # also sync config files to server
```

Server runs on DigitalOcean at `143.110.169.251`. Cron at `0 8 * * *` runs the AEC pipeline; `0 9 * * *` runs Startups. See [`docs/DEPLOY.md`](docs/DEPLOY.md).

## API Keys Required

| Key | Used For |
|---|---|
| `GOOGLE_MAPS_API_KEY` | Business discovery (AEC, Local Services) |
| `APOLLO_API_KEY` | Contact enrichment (all pipelines) |
| `ANTHROPIC_API_KEY` | Email personalization (AEC, Startups) |
| `INSTANTLY_API_KEY` | Email campaign execution (AEC, Startups) |
| `SERPAPI_API_KEY` | Hiring signal sourcing (Startups) |
| Google Sheets service account | CRM (all pipelines) |
