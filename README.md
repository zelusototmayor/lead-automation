# Lead Automation System

Multi-campaign outbound system: signal-based lead sourcing → **Instantly
SuperSearch** enrichment → Claude personalization → Instantly email
sequences. Managed via Google Sheets CRM + FastAPI dashboard.

> **Apr 2026 v2 restart** — Apollo dropped (code left dormant). Four
> dedicated cold inboxes, four fresh Instantly campaigns, four new
> `*_v2` CRM tabs.

## Campaigns

| Campaign | ICP | Sourcing | Outreach | Inbox | Daily Target |
|---|---|---|---|---|---|
| **AEC — Outbound System** | Architecture/Engineering firms, 5-200 employees (US) | Google Maps + SuperSearch | Email (4-step Instantly) | `ana@zeluautomations.com` | 15 |
| **Startups — Outbound System** | B2B SaaS 5-50 employees, hiring SDR/BDR | SerpAPI Jobs + SuperSearch | Email (4-step Instantly) | `jose@zeluautomations.com` | 15 |
| **EU B2B — Outbound System** | B2B companies hiring SDR/BDR in UK / ES / PT | Apify LinkedIn + SuperSearch | Email (4-step Instantly) | `ana@zelusotto.com` | 12 |
| **Logistics — Quote Agent** | SMB freight brokers / forwarders / 3PLs (UK + Spain) | Google Maps + SuperSearch | Email (4-step, built in Instantly) | `jose@zelusotto.com` | 15 |
| **Local Services** (phone) | Staffing, real estate, property mgmt, insurance | Google Maps | Cold calling (dashboard CRM) | — | 50 |
| **PT Logistics** (phone) | Logistics companies in Portugal | Google Maps | Cold calling (separate CRM) | — | 10 |

Logistics email copy lives **in Instantly**, not in repo YAML. No product
URLs in any cold email (spam risk) — `max@zelusottomayor.com` is the
try-it address for the Logistics offer.

See [`docs/CAMPAIGNS.md`](docs/CAMPAIGNS.md) for full ICP definitions,
signal logic, and email strategies per campaign.

## Project Structure

```
lead-automation/
├── src/
│   ├── main.py                      # AEC pipeline runner
│   ├── startups.py                  # B2B Startups pipeline runner
│   ├── eu_outreach.py               # EU B2B pipeline runner
│   ├── logistics.py                 # Logistics Quote Agent pipeline (UK+ES)
│   ├── local_services.py            # Local Services (phone) runner
│   ├── lead_sourcing/
│   │   ├── instantly_enrichment.py  # Instantly SuperSearch (Apollo replacement)
│   │   ├── apollo.py                # DORMANT — kept for backward compat
│   │   ├── google_maps.py           # Location-based business discovery
│   │   ├── serpapi.py               # SerpAPI Google Jobs hiring signals
│   │   └── apify.py                 # LinkedIn Jobs scraping (EU)
│   ├── crm/
│   │   ├── sheets.py                # CRM for email-first pipelines
│   │   └── local_services_sheet.py  # CRM for phone-first pipeline
│   └── outreach/
│       ├── personalize.py           # Claude email personalization + scoring
│       ├── instantly_client.py      # Instantly.ai V2 API client
│       └── sync_instantly.py        # Sync engagement → Google Sheets
├── dashboard/app/
│   ├── main.py                      # FastAPI dashboard (metrics + cold call CRM)
│   ├── auth.py                      # Simple auth
│   └── metrics.py                   # CRM metric calculations
├── config/
│   ├── settings.yaml                # All pipeline configs (env-templated keys)
│   └── email_templates.yaml         # AEC 4-email sequence (plain-text, no URLs)
├── scripts/
│   ├── deploy-automation.sh         # Build amd64 → push → pull on server
│   ├── run-startups.sh              # Cron wrapper for Startups pipeline
│   ├── run-eu-outreach.sh           # Cron wrapper for EU pipeline
│   ├── aec_instantly_burn.py        # One-shot SuperSearch credit burn (AEC)
│   └── requeue_polluted_to_v2.py    # Migrate polluted-window leads into v2 tabs
└── docs/
    ├── CAMPAIGNS.md                 # Per-campaign ICP, sourcing, outreach
    ├── ARCHITECTURE.md              # Data flows, module map, API cost ref
    └── DEPLOY.md                    # Server setup, deploy workflow, cron
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

# Run Startups pipeline
python src/startups.py --target 15

# Run EU pipeline
python src/eu_outreach.py --target 12

# Run Logistics pipeline
python src/logistics.py --target 15

# Run Local Services (phone)
python src/local_services.py --target 50

# Migrate polluted-window leads to v2 tabs (one-off)
python scripts/requeue_polluted_to_v2.py --dry-run
python scripts/requeue_polluted_to_v2.py
```

## Configuration

All settings live in `config/settings.yaml`. API keys are env-templated
(`${VARIABLE_NAME}`) and loaded from `.env`.

Per-campaign budgets use `supersearch_credit_budget` (replaces
`apollo_credit_budget`, which is kept at `0` but not deleted so old code
paths don't break).

Key settings per campaign:
- `lead_sourcing.daily_target` / `supersearch_credit_budget` — AEC
- `startups.daily_target` / `supersearch_credit_budget` — Startups
- `eu_outreach.daily_target` / `supersearch_credit_budget` — EU
- `logistics.daily_target` / `supersearch_credit_budget` — Logistics
- `local_services.daily_target` — Local Services (phone, no enrichment)

## Deploy

```bash
./scripts/deploy-automation.sh                # build + push + pull on server
./scripts/deploy-automation.sh --sync-config  # also sync config files
```

Server runs on DigitalOcean at `143.110.169.251`. Cron runs the pipelines
on weekdays; see [`docs/DEPLOY.md`](docs/DEPLOY.md).

## API Keys Required

| Key | Used For |
|---|---|
| `GOOGLE_MAPS_API_KEY` | Business discovery (AEC, Logistics, Local Services) |
| `INSTANTLY_API_KEY` | **SuperSearch enrichment + email campaign execution** |
| `ANTHROPIC_API_KEY` | Claude email personalization (AEC, Startups, EU) |
| `SERPAPI_API_KEY` | Hiring signal sourcing (Startups) |
| `APIFY_API_KEY` | LinkedIn Jobs scraping (EU) |
| Google Sheets service account | CRM (all pipelines) |
| `APOLLO_API_KEY` | **Dormant** — kept for backward compat, not called at runtime |
