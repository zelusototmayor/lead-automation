# Lead Sourcing & Outreach Automation System
## For Zelu Sottomayor's Automation Agency

---

## Executive Summary

This document outlines a fully automated daily workflow that:
1. Sources 5-10 qualified agency leads
2. Researches and enriches each lead
3. Updates your Google Sheets CRM
4. Sends personalized cold emails automatically
5. Runs on your DigitalOcean droplet via Docker

**The meta-pitch**: Your outreach emails can mention that the research, personalization, and sending were all automated—demonstrating your expertise firsthand.

---

## System Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                    DAILY CRON JOB (8:00 AM)                         │
└─────────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    LEAD SOURCING MODULE                             │
│  ┌─────────────────────┐    ┌─────────────────────┐                │
│  │   Google Maps API   │    │    Apollo.io API    │                │
│  │  (Local Agencies)   │    │  (B2B Enrichment)   │                │
│  └─────────────────────┘    └─────────────────────┘                │
│            │                          │                             │
│            └──────────┬───────────────┘                             │
│                       ▼                                             │
│              Deduplicate & Filter                                   │
└─────────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    RESEARCH & ENRICHMENT                            │
│  • Scrape agency website (services, team size indicators)           │
│  • Find decision-maker contacts via Apollo                          │
│  • Check for existing automation tools (competitor analysis)        │
│  • Score lead quality (1-10)                                        │
└─────────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    GOOGLE SHEETS CRM                                │
│  Columns: Company | Contact | Email | Phone | Website |             │
│           Industry | Size | Lead Score | Status | Date Added |      │
│           Last Contact | Notes | Email Sent | Response              │
└─────────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    EMAIL PERSONALIZATION (LLM)                      │
│  • Generate personalized opener based on research                   │
│  • Reference specific pain points for their industry                │
│  • Mention the automation meta-angle                                │
└─────────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    EMAIL SENDING (Gmail API)                        │
│  • Send via subdomain (outreach.yourdomain.com)                     │
│  • Respect daily limits (50/day max for cold email)                 │
│  • Randomized send times (looks human)                              │
│  • Track opens/responses in CRM                                     │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Component 1: Lead Sourcing

### Recommended Dual-Source Strategy

**Primary: Google Maps API**
- Cost: ~$17/1000 requests (Places API)
- Best for: Finding local/regional agencies
- Data: Business name, address, phone, website, ratings

**Secondary: Apollo.io**
- Cost: Free tier (50 emails/month) or Basic ($59/user/month)
- Best for: Contact enrichment, decision-maker emails
- Data: 210M+ contacts, job titles, company size, technologies used

### Search Strategy for Agencies

```python
AGENCY_SEARCH_QUERIES = [
    "marketing agency {city}",
    "PR agency {city}",
    "communications firm {city}",
    "sports marketing agency {city}",
    "media agency {city}",
    "digital agency {city}",
    "advertising agency {city}",
    "creative agency {city}",
    "branding agency {city}",
]

TARGET_CITIES = [
    "Miami", "New York", "Los Angeles", "Chicago", "Austin",
    "Atlanta", "Denver", "Seattle", "Boston", "San Francisco"
    # Add your target markets
]
```

### Filtering Criteria (Exclude)

- Automation/AI agencies (competitors)
- Companies already in CRM (dedupe)
- No website or contact info
- Less than 2 years in business (if detectable)

---

## Component 2: Research & Enrichment

### Data Points to Collect

| Field | Source | Purpose |
|-------|--------|---------|
| Company Name | Google Maps | Identification |
| Website | Google Maps | Research |
| Industry Vertical | Apollo/Manual | Personalization |
| Employee Count | Apollo | Qualifying |
| Decision Maker Name | Apollo | Personalization |
| Decision Maker Email | Apollo | Outreach |
| Decision Maker Title | Apollo | Personalization |
| Tech Stack | Apollo | Pain point identification |
| Recent News | Web Scrape | Conversation starter |
| Services Offered | Web Scrape | Relevance scoring |

### Lead Scoring Algorithm

```
Score (1-10) based on:
- Has website: +2
- Has verified email: +2
- Employee count 10-100: +2 (sweet spot for automation needs)
- Industry match (marketing/media/sports): +2
- No existing automation tools detected: +2
```

---

## Component 3: Google Sheets CRM Structure

### Sheet 1: Leads Master

| Column | Type | Description |
|--------|------|-------------|
| A: ID | Auto | Unique identifier |
| B: Company | Text | Agency name |
| C: Contact Name | Text | Decision maker |
| D: Email | Text | Primary contact email |
| E: Phone | Text | Business phone |
| F: Website | URL | Company website |
| G: Industry | Dropdown | Marketing/PR/Sports/Media/Other |
| H: Estimated Size | Number | Employee count |
| I: Lead Score | Number | 1-10 quality score |
| J: Status | Dropdown | New/Contacted/Replied/Meeting/Won/Lost |
| K: Date Added | Date | Auto-populated |
| L: Last Contact | Date | Auto-updated |
| M: Email 1 Sent | Checkbox | Initial outreach |
| N: Email 2 Sent | Checkbox | First follow-up |
| O: Email 3 Sent | Checkbox | Second follow-up |
| P: Response | Text | Any reply received |
| Q: Notes | Text | Research notes |
| R: Source | Text | Google Maps/Apollo/Referral |

### Sheet 2: Email Templates

Store your email templates with merge tags: {{first_name}}, {{company}}, {{industry}}, etc.

### Sheet 3: Daily Stats

Track: Leads added, Emails sent, Opens, Replies, Conversion rate

---

## Component 4: Email Strategy

### Domain Setup (CRITICAL)

**Before sending any cold emails:**

1. **Create a subdomain**: `outreach.yourdomain.com` or `hello.yourdomain.com`
2. **Set up authentication**:
   - SPF record
   - DKIM signing
   - DMARC policy
3. **Warmup period**: 4-6 weeks minimum for new domain
   - Week 1-2: 5-10 emails/day (warm contacts only)
   - Week 3-4: 15-25 emails/day
   - Week 5-6: 30-50 emails/day
   - Post-warmup: Max 50 cold emails/day

### Warmup Service Recommendation

Use a service like MailReach, TrulyInbox, or Lemwarm (~$29-49/month) to:
- Automatically send/receive warmup emails
- Build positive engagement signals
- Monitor deliverability health

### Email Sequence

**Email 1: Initial Outreach (Day 0)**
```
Subject: Quick question about {{company}}'s lead workflow

Hi {{first_name}},

I came across {{company}} while researching {{industry}} agencies
in {{city}}. [Personalized observation about their work].

Here's something fun: this email was researched, written, and sent
entirely through automation. That's what I do—I help agencies like
yours eliminate the tedious stuff so your team can focus on what
actually matters.

Curious what 10+ hours per week of manual work looks like automated?

Happy to show you a quick example relevant to {{industry}}.

Best,
Jose

P.S. Yes, even the research on your company was automated.
I practice what I preach.
```

**Email 2: Follow-up (Day 3)**
```
Subject: Re: Quick question about {{company}}'s lead workflow

Hi {{first_name}},

Following up on my note from Monday.

I've helped agencies automate:
• Lead intake → CRM updates (0 manual entry)
• Client reporting (auto-generated weekly)
• Invoice chasing (polite, persistent, automatic)

Which of these would save {{company}} the most headaches?

—Jose
```

**Email 3: Breakup (Day 7)**
```
Subject: Closing the loop

Hi {{first_name}},

I'll take the hint—timing probably isn't right.

If automation becomes a priority later, I'm at
jose@yourdomain.com.

Cheers,
Jose
```

### Sending Best Practices

- Send between 9-11 AM and 2-4 PM (recipient's timezone)
- Randomize send times (±30 minutes) to appear human
- Never send on weekends for B2B
- Monday and Tuesday typically have highest open rates
- Limit to 50 cold emails/day max to protect reputation

---

## Component 5: Technical Implementation

### Tech Stack

| Component | Technology | Purpose |
|-----------|------------|---------|
| Runtime | Python 3.11+ | Main automation |
| Lead Sourcing | Google Places API, Apollo API | Data collection |
| Web Scraping | BeautifulSoup, Playwright | Research |
| LLM | Claude API or GPT-4 | Personalization |
| CRM | Google Sheets API | Data storage |
| Email | Gmail API | Sending |
| Scheduling | Cron | Daily execution |
| Container | Docker | Deployment |
| Server | DigitalOcean Droplet | Hosting |

### Project Structure

```
lead-automation/
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── config/
│   ├── settings.yaml          # API keys, search params
│   ├── credentials.json       # Google OAuth (gitignored)
│   └── email_templates.yaml   # Email sequences
├── src/
│   ├── __init__.py
│   ├── main.py               # Daily orchestrator
│   ├── lead_sourcing/
│   │   ├── google_maps.py
│   │   └── apollo.py
│   ├── enrichment/
│   │   ├── web_scraper.py
│   │   └── research.py
│   ├── crm/
│   │   └── sheets.py
│   ├── email/
│   │   ├── personalize.py
│   │   └── sender.py
│   └── utils/
│       ├── deduplication.py
│       └── logging.py
├── tests/
│   └── ...
└── logs/
```

### Docker Configuration

```dockerfile
# Dockerfile
FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    chromium \
    chromium-driver \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "src/main.py"]
```

```yaml
# docker-compose.yml
version: '3.8'

services:
  lead-automation:
    build: .
    container_name: lead-automation
    volumes:
      - ./config:/app/config
      - ./logs:/app/logs
    environment:
      - GOOGLE_API_KEY=${GOOGLE_API_KEY}
      - APOLLO_API_KEY=${APOLLO_API_KEY}
      - OPENAI_API_KEY=${OPENAI_API_KEY}  # or ANTHROPIC_API_KEY
    restart: unless-stopped
```

### Cron Setup on DigitalOcean

```bash
# Add to crontab (crontab -e)
# Run daily at 8:00 AM server time
0 8 * * * cd /path/to/lead-automation && docker-compose run --rm lead-automation >> /var/log/lead-automation.log 2>&1
```

---

## Component 6: Setup Checklist

### APIs & Services Needed

- [ ] **Google Cloud Project**
  - [ ] Enable Places API
  - [ ] Enable Sheets API
  - [ ] Enable Gmail API
  - [ ] Create OAuth 2.0 credentials
  - [ ] Create Service Account for Sheets

- [ ] **Apollo.io Account**
  - [ ] Sign up (free tier available)
  - [ ] Get API key
  - [ ] Understand credit limits

- [ ] **Email Warmup Service**
  - [ ] Choose: MailReach, TrulyInbox, or Lemwarm
  - [ ] Set up subdomain
  - [ ] Configure DNS (SPF, DKIM, DMARC)
  - [ ] Start 4-6 week warmup

- [ ] **LLM API** (for personalization)
  - [ ] Anthropic Claude API or OpenAI GPT-4
  - [ ] Get API key

- [ ] **DigitalOcean Droplet**
  - [ ] Basic $6/month droplet sufficient
  - [ ] Install Docker
  - [ ] Clone repository
  - [ ] Set up environment variables

---

## Cost Breakdown (Monthly)

| Item | Cost | Notes |
|------|------|-------|
| Google Maps API | ~$50 | ~300 searches/day |
| Apollo.io | $0-59 | Free tier or Basic |
| Email Warmup | $29-49 | MailReach or similar |
| LLM API | ~$20 | ~10 emails/day personalized |
| DigitalOcean | $6 | Basic droplet |
| **Total** | **$105-184/mo** | |

**ROI Math**: One closed client likely worth $2,000-10,000+ covers months of operation.

---

## Risk Mitigation

### Email Deliverability

- Always use subdomain (protects main domain)
- Never exceed 50 cold emails/day
- Monitor bounce rate (keep under 2%)
- Remove bounces immediately
- Stop sending if spam complaints rise

### Legal Compliance (CAN-SPAM)

- Include physical address in emails
- Provide clear unsubscribe option
- Honor opt-outs within 10 business days
- Don't use misleading subject lines
- Identify message as advertisement if required

### Data Quality

- Verify emails before sending (use Apollo's verification or NeverBounce)
- Deduplicate against existing CRM
- Remove leads that bounce or complain

---

## Alternative Approaches Considered

### n8n (No-Code Alternative)

**Pros**: Faster setup, visual workflow, 1,700+ integrations
**Cons**: Less flexible for complex data cleaning, harder to customize LLM prompts

**Verdict**: n8n is excellent for rapid prototyping. You could start with n8n and migrate to custom Python if you need more control. There's even an existing n8n template for Apollo + AI + email outreach.

### Instantly.ai / Smartlead

**Pros**: Purpose-built for cold email, handles warmup, has built-in sequences
**Cons**: Less customization, monthly cost ($37-97/mo), less control over lead sourcing

**Verdict**: Good if you want to skip building email infrastructure. Can combine with custom lead sourcing.

---

## Next Steps

1. **Week 1**: Set up subdomain and start email warmup
2. **Week 2**: Set up Google Cloud APIs and Apollo account
3. **Week 3**: Build and test lead sourcing module locally
4. **Week 4**: Build CRM integration and email personalization
5. **Week 5**: Deploy to DigitalOcean, test end-to-end
6. **Week 6+**: Email warmup complete, start live outreach

---

## Questions to Resolve

1. **Target geography**: Which cities/regions should we prioritize first?
2. **Email sending account**: Using your personal Gmail or Google Workspace?
3. **LLM preference**: Claude API or OpenAI for personalization?
4. **Volume ramp**: Start with 5 leads/day or fewer during initial testing?

---

*This system demonstrates your automation expertise by being itself an automated system—every lead you contact will know they were found, researched, and reached through the very services you're selling.*
