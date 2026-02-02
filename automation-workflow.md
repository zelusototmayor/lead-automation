# Lead Automation Chronogram

## Daily Schedule

```
┌─────────────────────────────────────────────────────────────────────┐
│  8:00 AM  │  Daily Trigger (via cron/docker)                        │
├───────────┼─────────────────────────────────────────────────────────┤
│  Hourly   │  Reply Sync (scripts/sync_replies.py)                   │
└───────────┴─────────────────────────────────────────────────────────┘
```

---

## PHASE 1: Sync from Instantly (8:00 AM)

```
Instantly API
    │
    ├── Fetch all campaigns
    ├── Get leads with engagement data (opens, clicks, replies)
    └── Update Google Sheets CRM
            ├── Opens count
            ├── Clicks count
            ├── Status → "Replied" if reply detected
            └── Instantly Status
```

---

## PHASE 2: Lead Sourcing (~8:01 AM)

```
┌──────────────────────────────────────────────────────────────┐
│  STEP 1: Google Maps Search                                  │
│  ─────────────────────────────────────────────────────────── │
│  • Shuffles 15 US cities (NY, LA, Miami, Chicago, etc.)      │
│  • Picks 3 random queries per city:                          │
│    - "marketing agency", "digital marketing agency"          │
│    - "PR agency", "advertising agency", etc.                 │
│  • Gets place details: name, website, phone, rating          │
│  • Filters out: AI/software/web dev agencies                 │
└──────────────────────────────────────────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────────────┐
│  STEP 2: Apollo Enrichment                                   │
│  ─────────────────────────────────────────────────────────── │
│  For each agency with website:                               │
│  • Organization search → industry, employees, description    │
│  • Contact search → owners, founders, C-suite, VPs           │
│  • Get verified emails for decision-makers                   │
└──────────────────────────────────────────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────────────┐
│  STEP 3: Lead Scoring (0-10)                                 │
│  ─────────────────────────────────────────────────────────── │
│  +2 verified email  │  +2 sweet spot (10-100 employees)      │
│  +1 has website     │  +2 matching industry                  │
│  +1 has phone       │  +1 has LinkedIn                       │
└──────────────────────────────────────────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────────────┐
│  STEP 4: Add to Google Sheets CRM                            │
│  ─────────────────────────────────────────────────────────── │
│  • Deduplication check (email + company/city)                │
│  • Creates row with all data                                 │
│  • Status = "New"                                            │
│  • Target: ~10 new leads/day                                 │
└──────────────────────────────────────────────────────────────┘
```

---

## PHASE 3: Email Personalization (~8:05 AM)

```
┌──────────────────────────────────────────────────────────────┐
│  STEP 1: Get "New" leads from CRM                            │
│  ─────────────────────────────────────────────────────────── │
│  Filter: Status = "New" AND Email 1 Sent = FALSE             │
└──────────────────────────────────────────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────────────┐
│  STEP 2: Claude AI Personalization                           │
│  ─────────────────────────────────────────────────────────── │
│  Sends to Claude:                                            │
│  • Your sender bio + value proposition                       │
│  • Lead context (company, industry, size, description)       │
│                                                              │
│  Claude generates:                                           │
│  • personalized_opener (references specific details)         │
│  • specific_pain_point (outbound/sales challenges)           │
│  • industry_specific_insight                                 │
│  • suggested_subject line                                    │
└──────────────────────────────────────────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────────────┐
│  STEP 3: Add to Instantly Campaign                           │
│  ─────────────────────────────────────────────────────────── │
│  • Adds lead with custom fields (personalization data)       │
│  • Updates CRM: Status = "Queued"                            │
└──────────────────────────────────────────────────────────────┘
```

---

## PHASE 4: Instantly Sends Emails (9AM-5PM, Mon-Fri)

```
┌─────────────────────────────────────────────────────────────────────┐
│  EMAIL SEQUENCE (Automatically by Instantly)                        │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  Day 0   │  Email 1: Initial Outreach                              │
│          │  "This email found you, researched you, wrote itself..."│
│          │                                                          │
│  Day 3   │  Email 2: First Follow-up                               │
│          │  Lists 4 automation opportunities                        │
│          │                                                          │
│  Day 6   │  Email 3: Value Add                                     │
│          │  Industry-specific insight + other automation offers     │
│          │                                                          │
│  Day 10  │  Email 4: Breakup                                       │
│          │  Polite close, door stays open                           │
│                                                                     │
├─────────────────────────────────────────────────────────────────────┤
│  Settings: 50 emails/day max, 120s delay between sends             │
└─────────────────────────────────────────────────────────────────────┘
```

---

## PHASE 5: Reply Sync (Every Hour)

```
scripts/sync_replies.py
         │
         ├── Fetch all leads from Instantly campaigns
         ├── Find leads with replies
         └── Update CRM for each reply:
                 ├── Response = "Replied on [date]"
                 └── Status = "Replied"
```

---

## Visual Flow Summary

```
    ┌─────────────┐
    │ Google Maps │
    │  (Search)   │
    └──────┬──────┘
           │
           ▼
    ┌─────────────┐
    │   Apollo    │
    │ (Enrich +   │
    │  Contacts)  │
    └──────┬──────┘
           │
           ▼
    ┌─────────────┐
    │   Google    │◄────────────────┐
    │   Sheets    │                 │
    │    (CRM)    │                 │
    └──────┬──────┘                 │
           │                        │
           ▼                        │
    ┌─────────────┐                 │
    │  Claude AI  │                 │
    │(Personalize)│                 │
    └──────┬──────┘                 │
           │                        │
           ▼                        │
    ┌─────────────┐    Sync        │
    │  Instantly  │────────────────┘
    │  (Send +    │  (opens, clicks,
    │   Track)    │   replies)
    └─────────────┘
```

---

## Monthly Costs

| Service | Cost |
|---------|------|
| Google Maps API | ~$50 |
| Apollo.io | $0-59 |
| Claude API | ~$20 |
| DigitalOcean | $6 |
| Email Warmup | $29-49 |
| **Total** | **~$105-185/mo** |

---

## Key Files

| File | Purpose |
|------|---------|
| `src/main.py` | Main orchestrator, runs all phases |
| `src/lead_sourcing/google_maps.py` | Google Maps API client |
| `src/lead_sourcing/apollo.py` | Apollo.io enrichment client |
| `src/crm/sheets.py` | Google Sheets CRM management |
| `src/outreach/personalize.py` | Claude AI email personalization |
| `src/outreach/instantly_client.py` | Instantly.ai campaign management |
| `src/outreach/sync_instantly.py` | Sync engagement metrics from Instantly |
| `scripts/sync_replies.py` | Standalone hourly reply sync script |
| `config/settings.yaml` | Cities, search queries, API config |
| `config/email_templates.yaml` | Email sequences & personalization prompts |

---

The entire system runs automatically once deployed - you just show up to calls from interested leads.
