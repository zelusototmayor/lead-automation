# LinkedIn Outbound System — Architecture Plan (v2)

**ICP:** B2B SaaS companies hiring SDR/BDR roles (from Apify bulk scrape)
**Channel:** LinkedIn connection requests (no note) → acceptance monitoring → personalized DM sequence
**Goal:** 20 connection requests/day (max 100/week), monitor acceptances, DM with direct pitch

---

## System Overview

```
┌─────────────────────────────────────────────────────────────┐
│                    DATA LAYER (already built)                │
│                                                             │
│  Apify Bulk Scrape (Apr 8)                                  │
│  └─ ~7,000-10,000 B2B companies hiring SDRs                │
│  └─ Company name, LinkedIn URL, job title, location         │
│  └─ Stored in Google Sheets: "Apify Bulk YYYY-MM-DD" tab   │
└──────────────────────────┬──────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────┐
│          PROSPECT FINDER (browser automation — daily)        │
│                                                             │
│  For each company from Apify data:                          │
│  1. Open company LinkedIn page → People tab                 │
│  2. Apply decision-maker selection criteria (see below)     │
│  3. Extract prospect's LinkedIn profile URL                 │
│  4. Store in Google Sheets "LinkedIn Outbound" tab          │
│                                                             │
│  Tool: Claude in Chrome (browser automation)                │
│  Output: 25 prospect LinkedIn profile URLs per day          │
└──────────────────────────┬──────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────┐
│          CONNECTION REQUESTER (browser automation — daily)   │
│                                                             │
│  Runs: Daily at 10:00 AM, weekdays only                     │
│                                                             │
│  Action:                                                    │
│  1. Read 20 prospects with status "New" from Sheets         │
│  2. Open each LinkedIn profile                              │
│  3. Click "Connect" → Send (NO connection note)             │
│  4. Update status to "Request Sent" + timestamp             │
│                                                             │
│  ⚠ NO CONNECTION NOTE — LinkedIn limits to 5 notes/month   │
│  Connection requests go out blank.                          │
│                                                             │
│  Safety limits:                                             │
│  - Max 20 requests/day                                      │
│  - Max 100 requests/week (hard stop)                        │
│  - Random delays between requests (45-120 seconds)          │
│  - Only during business hours                               │
│  - Stop immediately if LinkedIn shows warning               │
│  - Warm-up: 10/day week 1, 15/day week 2, 20/day week 3+   │
└──────────────────────────┬──────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────┐
│         ACCEPTANCE MONITOR (dual-source — daily)            │
│                                                             │
│  Runs: Daily at 10:15 AM and 3:00 PM, weekdays              │
│                                                             │
│  TWO input sources:                                         │
│                                                             │
│  Source 1 — LinkedIn notifications:                         │
│  - Open LinkedIn notifications page                         │
│  - Scan for "accepted your connection request"              │
│  - Match to prospect in Sheets → mark "Connected"           │
│                                                             │
│  Source 2 — Manual CRM toggle:                              │
│  - Jose may see acceptance notifications first during       │
│    daily LinkedIn use                                       │
│  - He toggles "Connected?" = YES in the Sheets CRM         │
│  - Monitor picks this up and flags for DM sending           │
│                                                             │
│  Both sources → set status "Connected" → queue for DM 1     │
│                                                             │
│  Expected acceptance rate: 30-40%                           │
│  Timeline: Most acceptances within 48-72 hours              │
└──────────────────────────┬──────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────┐
│           DM SENDER (browser automation — daily)            │
│                                                             │
│  Runs: Daily at 10:30 AM, weekdays                           │
│  Tone: Direct and confident                                 │
│  Personalization: Claude API — mention specific industry,   │
│  company name, and the exact role they're hiring for        │
│                                                             │
│  DM sequence (3 messages):                                  │
│                                                             │
│  MSG 1 (on first check after acceptance):                   │
│  Personalized by Claude using:                              │
│  - Company name + what they do                              │
│  - The specific SDR/BDR role they're hiring                 │
│  - Their industry context                                   │
│  Template direction:                                        │
│  "{first_name}, I build automated outbound systems for      │
│  {industry} companies. I saw {company} is hiring            │
│  {job_title} — I help teams like yours get the same         │
│  pipeline output without the headcount. Happy to show       │
│  you how it works."                                         │
│                                                             │
│  MSG 2 (5 days after MSG 1, if no reply):                   │
│  Personalized with a specific result or angle:              │
│  "Quick follow-up — we recently built a system for a        │
│  {industry} company that books 15+ qualified meetings       │
│  a month, fully automated. No SDRs, no VAs. Would a        │
│  15-minute walkthrough be worth your time?"                 │
│                                                             │
│  MSG 3 (10 days after MSG 2, if no reply):                  │
│  Short, no-pressure close:                                  │
│  "Last one from me — if scaling outbound is a priority      │
│  down the road, I'm around. Good luck with the             │
│  {job_title} hire."                                         │
│                                                             │
│  Safety: Max 40 DMs/day, random delays, business hours      │
└──────────────────────────┬──────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────┐
│         REPLY MONITOR (browser automation — daily)          │
│                                                             │
│  Runs: Daily at 10:45 AM and 4:00 PM, weekdays              │
│  Action: Check LinkedIn messaging inbox for new messages    │
│  - Match to prospects in Sheets                             │
│  - Update status to "Replied" with reply preview            │
│  - Send ntfy.sh push notification to Jose's phone           │
└─────────────────────────────────────────────────────────────┘
```

---

## Decision-Maker Selection Criteria

Repeatable process for choosing WHO to connect with at each company.
Based on company size (from Apify data or LinkedIn company page).

### Step 1: Determine company size

Check the company LinkedIn page for employee count. Then apply:

| Company Size | Target Person | Why |
|---|---|---|
| **Under 20 employees** | Founder / CEO / Co-Founder | They ARE the sales leader. No VP layer yet. |
| **20-50 employees** | VP of Sales / Head of Sales / Head of Growth | They own the outbound budget and hire the SDRs. Fallback: CEO → CRO. |
| **50+ employees** | CRO / VP of Sales / Director of Sales | They control outbound strategy. Fallback: VP Growth → Head of Revenue. |

### Step 2: Find the person on LinkedIn

1. Go to the company LinkedIn page → "People" tab
2. Search by title keywords matching the size tier above
3. If multiple matches, pick the one who:
   - Has "Sales", "Revenue", "Growth", or "Business Development" in their title
   - Has been at the company 6+ months (not brand new)
   - Has a profile photo and activity (signals they're active on LinkedIn)
4. If no match on primary titles, fall back to next tier

### Step 3: Validate before connecting

Skip the prospect if:
- They have "Hiring" or "Recruiter" in their headline (wrong person)
- They're in a different country than the job posting
- Their profile shows they left the company recently
- They're already a 1st-degree connection

### Selection priority (title keywords to search, in order):

**Under 20 employees:**
1. "Founder" or "Co-Founder"
2. "CEO" or "Chief Executive"
3. "Owner" or "Managing Director"

**20-50 employees:**
1. "VP Sales" or "VP of Sales"
2. "Head of Sales" or "Head of Growth"
3. "Sales Director" or "Director of Sales"
4. "CEO" or "Founder" (fallback)

**50+ employees:**
1. "CRO" or "Chief Revenue Officer"
2. "VP Sales" or "VP of Sales"
3. "Director of Sales" or "Director of Business Development"
4. "Head of Revenue" or "Head of Sales" (fallback)

---

## Google Sheets CRM — "LinkedIn Outbound" Tab

### Columns

| Column | Type | Description |
|---|---|---|
| ID | Auto | LKDN-YYYYMMDDHHMMSS |
| Company | Text | Company name (from Apify) |
| Contact Name | Text | Decision-maker full name |
| Title | Text | Their job title |
| LinkedIn URL | URL | Prospect's personal LinkedIn profile |
| Company LinkedIn | URL | Company LinkedIn page |
| Job Hiring | Text | SDR/BDR role they're posting (from Apify) |
| Country | Text | Country (from Apify) |
| Employee Count | Text | Estimated company size |
| Industry | Text | From Apify job description |
| Status | Text | New / Request Sent / Connected / DM 1 / DM 2 / DM 3 / Replied / No Reply |
| **Connected? (manual)** | **Checkbox** | **Jose toggles YES when he sees acceptance first** |
| Connection Sent | Date | When request was sent |
| Connection Accepted | Date | When acceptance detected |
| DM 1 Sent | Date | When MSG 1 was sent |
| DM 2 Sent | Date | When MSG 2 was sent |
| DM 3 Sent | Date | When MSG 3 was sent |
| Reply | Text | Reply message preview |
| Reply Date | Date | When reply was received |
| Notes | Text | Any notes |
| Source | Text | "apify_bulk_scrape" |

### Status Flow

```
New
 └─→ Request Sent (connection_requester.py)
      └─→ Connected (acceptance_monitor.py OR manual toggle)
           └─→ DM 1 (dm_sender.py — immediate)
                └─→ DM 2 (dm_sender.py — 5 days later)
                     └─→ DM 3 (dm_sender.py — 10 days later)
                          └─→ No Reply (after DM 3 + 7 days)
           └─→ Replied (at any point → reply_monitor.py)
```

### Manual Toggle Workflow

When Jose sees an acceptance notification on LinkedIn before the system does:
1. Open Google Sheets → "LinkedIn Outbound" tab
2. Find the prospect row
3. Check the **"Connected? (manual)"** checkbox
4. The next acceptance_monitor.py run picks this up, sets status to "Connected", and queues DM 1

---

## Cron Schedule

All jobs run at **10:00 AM block** (Jose is at his computer). Weekdays only.

| Time | Script | Action |
|---|---|---|
| 10:00 AM | prospect_finder.py | Find 25 decision-maker LinkedIn profiles |
| 10:10 AM | connection_requester.py | Send 20 connection requests (no note) |
| 10:15 AM | acceptance_monitor.py | Check LinkedIn + CRM toggle for acceptances |
| 10:30 AM | dm_sender.py | Send DMs (msg 1, 2, or 3 — personalized by Claude) |
| 10:45 AM | reply_monitor.py | Check inbox for replies, notify via ntfy |
| 3:00 PM | acceptance_monitor.py | Second acceptance check |
| 4:00 PM | reply_monitor.py | End-of-day reply check |

---

## DM Personalization (Claude API)

Each DM is generated by Claude using context from the Apify data:

**Input to Claude:**
- Company name
- What they do (from job description snippet)
- The specific role they're hiring (e.g., "SDR — EMEA region")
- Their country/location
- The contact's name and title
- Message number (1, 2, or 3)

**Prompt direction:**
- Tone: Direct, confident, no fluff
- Always reference the specific hiring signal
- Mention something about their industry or product
- Keep under 300 characters for MSG 1, 200 for MSG 2-3
- No "I hope this finds you well" or generic openers
- End with a clear call to action (MSG 1-2) or graceful exit (MSG 3)

**Example outputs (what Claude should produce):**

MSG 1 for a fintech company hiring an SDR in London:
> "{first_name}, I build automated outbound systems for fintech companies. Saw {company} is hiring an SDR for EMEA — I help teams generate the same pipeline without the headcount. Happy to show you how it works in 15 min."

MSG 2 follow-up:
> "Quick follow-up — we recently helped a payments company go from 0 to 20 booked meetings/month with no SDRs. Would a short walkthrough be worth your time?"

MSG 3 close:
> "Last note — if scaling outbound becomes a priority, I'm around. Good luck with the SDR hire."

---

## Expected Funnel (Monthly)

| Stage | Volume | Rate |
|---|---|---|
| Prospects found | 625/month | 25/day × 25 days |
| Connection requests sent | 500/month | 20/day × 25 days |
| Weekly cap respected | 100/week max | ✓ |
| Connections accepted | 175/month | 35% |
| DM 1 sent | 175/month | 100% of accepted |
| Replies to DM 1 | 35/month | 20% |
| Replies to DM 2 | 14/month | 10% of remaining |
| Replies to DM 3 | 5/month | 4% of remaining |
| **Total conversations** | **~54/month** | **11% of requests** |
| Meetings booked | ~10-15/month | ~25% of conversations |

---

## Required APIs & Tools

| Tool | Purpose | Cost | Auth |
|---|---|---|---|
| Claude in Chrome | All LinkedIn browser automation | Free | Browser session |
| Claude API | DM personalization | Existing Anthropic key | API key (have) |
| Google Sheets API | CRM tracking | Free | Service account (have) |
| ntfy.sh | Reply notifications | Free | Topic URL (have) |
| Apify (one-time) | Bulk scrape Apr 8 | ~$20 remaining credits | API key (have) |

**No new paid subscriptions. Zero ongoing cost beyond Claude API tokens (~$2-5/month for DM personalization).**

---

## Risk Mitigation

**LinkedIn account restrictions:**
- Strict 20/day, 100/week connection limit
- Warm-up period: 10/day (week 1) → 15/day (week 2) → 20/day (week 3+)
- Random delays between all actions (45-120 seconds)
- Business hours only (10 AM - 5 PM)
- Stop all automation if LinkedIn shows any warning
- No connection notes (avoids the 5/month limit entirely)

**Message quality:**
- Every DM personalized by Claude (no templates sent as-is)
- Reference the specific job posting from Apify data
- Keep messages short and direct
- Track reply rates per message variant to improve over time

**Data freshness:**
- Apify "posted in last month" data stays relevant ~2-3 months
- After that, consider a $5-10 mini-scrape or free job board scraping
- Companies still hiring after 2 months are actually better prospects (harder to fill = more pain)

**Manual override:**
- Jose can toggle connections manually in Sheets at any time
- System checks both LinkedIn and CRM toggle
- No actions taken without data in Sheets (single source of truth)

---

## Files to Create

```
src/linkedin/
├── __init__.py
├── config.py               # Selection criteria, safety limits, message templates
├── prospect_finder.py      # Browser: find decision-maker LinkedIn URLs
├── connection_requester.py # Browser: send daily connection requests (no note)
├── acceptance_monitor.py   # Browser + Sheets: detect accepted connections
├── dm_sender.py            # Browser + Claude: send personalized DM sequence
└── reply_monitor.py        # Browser + Sheets: monitor replies, notify
```

---

## Build Order

1. **April 8** — Run Apify bulk scrape (data foundation)
2. **Phase 1** — Build Sheets CRM tab "LinkedIn Outbound" + config.py + prospect_finder.py
3. **Phase 2** — Build connection_requester.py (core outbound action)
4. **Phase 3** — Build acceptance_monitor.py (with manual toggle support)
5. **Phase 4** — Build dm_sender.py (Claude-personalized messages)
6. **Phase 5** — Build reply_monitor.py + ntfy notifications
7. **Phase 6** — Set up Cowork scheduled tasks (cron jobs)
8. **Week 1** — Warm-up: 10 connections/day, monitor behavior
9. **Week 2** — Ramp to 15/day
10. **Week 3+** — Full speed: 20/day
