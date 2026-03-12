# Campaign Reference

Each campaign has its own ICP, sourcing logic, enrichment budget, and outreach strategy. All share the same infrastructure (Apollo, Sheets, Claude, Instantly).

---

## Campaign 1: AEC Business Development

**Entry point:** `src/main.py`
**CRM tab:** `AEC Leads`
**Instantly campaign:** `AEC Business Development`

### ICP
- Architecture, engineering, environmental consulting firms
- 5–200+ employees
- US metros (15 target cities)
- Decision-maker: Principal, Director, VP of Business Development

### Sourcing
- **Source:** Google Maps Places API (text search by city)
- **Queries:** "civil engineering firm", "MEP firm", "structural engineering", "environmental consulting", "geotechnical", "land surveying", "architecture firm", "urban planning"
- **Exclude:** AECOM, Jacobs, WSP, Arcadis, universities, government agencies

### Enrichment
- Apollo.io org lookup → find decision-maker (seniority: director/vp/c_suite)
- Apollo.io person enrichment → verified email (costs 1 credit per lead)
- **Budget:** 200 Apollo credits/run

### Outreach
- 4-email Instantly.ai sequence (Day 0, 3, 7, 12)
- Personalized by Claude: opener, pain point, insight, subject
- Templates in `config/email_templates.yaml`
- **Pain points:** Over-reliance on referrals, principals stretched thin, inconsistent pipeline, feast/famine cycles

### Lead Scoring (1–10)
| Signal | Points |
|---|---|
| Has verified email | +2 |
| Has website | +1 |
| Has phone | +1 |
| Employee count 10–200 | +2 |
| Industry match (AEC keywords) | +2 |
| Has LinkedIn URL | +1 |

---

## Campaign 2: Local Services

**Entry point:** `src/local_services.py`
**CRM tab:** `Local Services`
**Outreach:** Phone (no Instantly)

### ICP
- US-based companies in high-value service verticals
- Verticals: recruiting/staffing, real estate, logistics/freight, property management, insurance
- Decision-maker: owner, director, manager (reachable by phone)

### Sourcing
- **Source:** Google Maps (by vertical + metro)
- **Metros:** New York, LA, Chicago, Dallas, Houston, Atlanta, Miami, Phoenix, Denver, Charlotte

**Queries by vertical:**
- Recruiting: "staffing agency", "recruiting firm", "executive search", "headhunting"
- Real Estate: "real estate brokerage", "commercial real estate firm"
- Logistics: "freight broker", "third party logistics", "trucking company", "supply chain"
- Property Mgmt: "property management company", "HOA management", "apartment management"
- Insurance: "insurance agency", "insurance brokerage", "independent insurance"

### Enrichment
- Apollo.io FREE people search — POC name + title only (no email, no credits)

### Outreach
- Phone-first: manual cold calling from dashboard CRM
- Dashboard: `GET /cold-calling` with call queue, log call outcomes, set follow-ups
- No email sequence (Instantly not used)

### CRM Schema
`ID | Company | POC Name | POC Title | Phone | Call Status | Notes | Follow-up | Email | Website | City | State | Vertical | Date Added | Status`

---

## Campaign 3: B2B Startups

**Entry point:** `src/startups.py`
**CRM tab:** `B2B Startups`
**Instantly campaign:** `B2B Startups Outbound`

### ICP
- B2B SaaS companies, 5–50 employees
- Actively hiring sales roles (SDR/BDR/AE) OR already have sales team
- Founders/CEOs reachable by email
- Industries: information technology, computer software, internet, fintech, logistics tech, telecom, security, networking

### Sourcing (3 signals)
1. **Apollo org search (free):** Companies hiring SDR/BDR/AE titles → `hiring` signal
2. **Apollo people search (free):** Companies with SDRs/BDRs already on staff → `has_sdrs` signal
3. **SerpAPI Google Jobs:** Job listings for "SDR", "BDR", "Sales Development Representative" → `google_jobs` signal
- Companies appearing in 2+ sources → `multi_signal=True` → prioritized
- **SerpAPI budget:** 30 searches/run (250/month limit)

### Filtering
- Industry must be in B2B whitelist (or have B2B keyword: saas, b2b, platform, api, cloud, enterprise, automation)
- Employee count: 5–50
- Exclude: Google, Amazon, Microsoft, Meta, Salesforce, Cisco, Oracle, IBM, SAP, etc.

### Enrichment
- Apollo.io org lookup → full company data
- Apollo.io person enrichment → founder/CEO email (costs 1 credit)
- **Budget:** 50 Apollo credits/run

### Outreach
- Instantly.ai email sequence
- Claude personalization includes `signal_hook` (references the hiring/SDR signal in email copy)

### Lead Scoring (1–10)
| Signal | Points |
|---|---|
| Multi-signal (2+ sources) | +3 |
| Hiring signal | +2 |
| Has SDRs signal | +1 |
| Sweet-spot employees (10–30) | +2 |
| Has verified email | +1 |
| Has website | +1 |
| B2B keywords in description | +1 |

---

## Adding a New Campaign

Checklist:
1. Define ICP, signal logic, sourcing source(s)
2. Create pipeline runner: `src/<campaign_name>.py`
3. Add config block to `config/settings.yaml`
4. Add/reuse CRM sheet: new tab in Google Sheet or new `GoogleSheetsCRM` instance with custom headers
5. Add email templates to `config/email_templates.yaml` (if email-first)
6. Create Instantly campaign + run `scripts/setup_<campaign>_campaign.py`
7. Add cron entry to server (or add to `run.sh`)
8. Update `docs/CAMPAIGNS.md`
