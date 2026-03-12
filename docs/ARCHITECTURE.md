# Architecture Reference

## Data Flow (Email-First Pipelines)

```
Sourcing                  Enrichment              CRM              Outreach
─────────                 ──────────              ───              ────────
Google Maps               Apollo.io               Google           Claude AI
SerpAPI        ──────►    Org lookup    ──────►    Sheets   ──────► Personalize
Apollo (free)             Email enrich            (tab)            │
                          (credits)                                ▼
                                                              Instantly.ai
                                                              (sequences)
                                                                   │
                                                              ◄────┘
                                                         Nightly sync
                                                    (opens/clicks/replies
                                                      → Google Sheets)
```

## Module Map

### `src/lead_sourcing/`

| Module | Class | Key Methods | Cost |
|---|---|---|---|
| `google_maps.py` | `GoogleMapsClient` | `search_businesses()`, `get_place_details()` | Pay-per-use (~$7/1k) |
| `apollo.py` | `ApolloClient` | `search_hiring_organizations()`, `search_companies_with_sdrs()`, `find_contacts_free()` (free); `find_contacts()`, `_enrich_person_by_id()` (paid) | Free searches; ~1 credit/email |
| `serpapi.py` | `SerpAPIClient` | `search_hiring_signals()`, `search_funding_signals()` | 250 searches/month free |

### `src/crm/`

| Module | Class | Used By | Key Features |
|---|---|---|---|
| `sheets.py` | `GoogleSheetsCRM` | AEC, Startups | Email dedup, Instantly sync, rate limiting, caching |
| `local_services_sheet.py` | `LocalServicesCRM` | Local Services | Phone tracking, call logging, follow-up queue |

**GoogleSheetsCRM schema:**
`ID | Company | Contact Name | Email | Phone | Status call | Notes | Website | Industry | Employee Count | City | Country | Lead Score | Status | Date Added | Last Contact | Email 1/2/3/4 Sent | Opens | Clicks | Response | Title | Instantly Status | Source | LinkedIn`

**LocalServicesCRM schema:**
`ID | Company | POC Name | POC Title | Phone | Call Status | Notes | Follow-up | Email | Website | City | State | Vertical | Date Added | Status`

### `src/outreach/`

| Module | Class/Function | Purpose |
|---|---|---|
| `personalize.py` | `EmailPersonalizer` | Claude AI → personalized_opener, specific_pain_point, industry_specific_insight, suggested_subject |
| `personalize.py` | `calculate_lead_score()` / `calculate_startup_lead_score()` | Score 1–10 for prioritization |
| `instantly_client.py` | `InstantlyClient` | Instantly V2 API: add leads, set sequences/schedules |
| `sync_instantly.py` | `InstantlySyncer` | Pull opens/clicks/replies/status from Instantly → update Sheets |

### `dashboard/app/`

| Route | Purpose |
|---|---|
| `GET /` | Main metrics dashboard |
| `GET /cold-calling` | Phone outreach CRM |
| `GET /api/metrics` | Live funnel stats (new/contacted/replied/won/lost) |
| `GET /api/cold-calling/leads` | Call queue with filters |
| `POST /api/cold-calling/log-call` | Record call outcome + notes |
| `POST /api/sync-replies` | Manual Instantly sync trigger |

## Key Design Patterns

### Apollo Credit Budgeting
`ApolloClient` tracks `_credits_used` vs `_credit_budget`. Stops enrichment when budget is hit. Budget is set per pipeline run (200 for AEC, 50 for Startups).

### Google Sheets Rate Limiting
- All rows loaded into `_cache` on init (single API call)
- `_find_in_cache()` for fast dedup (no additional API call)
- `_throttle()`: 1.5s minimum between writes
- Exponential backoff on 429/500/503 errors

### Multi-Signal Dedup (Startups)
Companies from 3 sources are deduped by name, signals merged. `multi_signal=True` if 2+ sources. List sorted: multi-signal + hiring signals first.

### Status Lifecycle
- Email pipelines: `New → Queued → Contacted → Replied → Won/Lost`
- Phone pipeline: `New → Contacted → [outcome states]`
- Status only promotes forward (no automatic downgrades)

## External API Summary

| API | Purpose | Rate Limit | Cost Model |
|---|---|---|---|
| Google Maps | Business discovery | 1000 QPS | ~$7/1k calls |
| Apollo.io | Enrichment + contact finding | ~10 RPS | 1 credit/email; free org/people search |
| SerpAPI | Hiring/funding signals | 250/month | Free tier (250/month) |
| Instantly.ai | Email campaign execution | V2 bearer | $99/month |
| Google Sheets | CRM storage | 60 req/min | Free |
| Anthropic | Email personalization | By plan | ~$0.003/1k in, $0.015/1k out |

## Config Reference (`config/settings.yaml`)

```yaml
api_keys:               # All env-templated (${VAR_NAME})
google_sheets:
  spreadsheet_id:       # Single sheet, multiple tabs
  sheet_name:           # AEC Leads tab
lead_sourcing:          # AEC settings (daily_target, credit_budget, cities, queries)
instantly:              # Campaign name + API key
personalization:        # Claude model, value prop, sender bio
local_services:         # Local Services settings (verticals, metros, queries)
startups:               # B2B Startups settings (filters, budgets, signal queries)
```
