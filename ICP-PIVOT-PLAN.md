# ICP Pivot: Agencies → AEC Professional Services

## Summary

Pivoting from marketing/advertising agencies to **AEC professional services firms** (engineering, architecture, environmental consulting). The system architecture stays the same — we're changing targeting, messaging, and scoring.

---

## What stays the same

- **Full workflow**: Google Maps → Apollo enrichment → Claude personalization → Instantly sequences → Sheets CRM sync
- **All source code modules**: `google_maps.py`, `apollo.py`, `personalize.py`, `instantly_client.py`, `sync_instantly.py`, `sheets.py`
- **Infrastructure**: Docker image, DigitalOcean server, cron schedule, deploy script
- **Instantly account**: Same account, new campaign (old one paused)
- **Google Sheets CRM**: Same spreadsheet, **new tab** "AEC Leads" (keeps agency data archived for reference)

---

## What changes

### 1. `config/settings.yaml`

| Section | Current (Agencies) | New (AEC) |
|---|---|---|
| **search_queries** | "marketing agency", "digital marketing agency", "PR agency", etc. (10 queries) | "civil engineering firm", "structural engineering firm", "MEP engineering firm", "environmental consulting firm", "architecture firm", "geotechnical engineering firm", "land surveying company", "environmental remediation company", "engineering consulting firm" (9 queries) |
| **exclude_keywords** | "automation", "AI agency", "software development", "web development", "app development" | "AECOM", "Jacobs", "WSP", "Arcadis", "Stantec", "Kimley-Horn", "HDR", "Gensler", "university", "college", "government" (exclude mega-firms + institutions — we want 5-50 employee firms) |
| **campaign_name** | "Agency Outreach" | "AEC Business Development" |
| **value_proposition** | "I build fully automated outbound systems for agencies..." | Rewritten for AEC — see below |
| **sender_bio** | "Zelu Sottomayor builds automated outbound systems for agencies..." | Rewritten for AEC — see below |
| **sheet_name** | "Leads" | "AEC Leads" |
| **target_cities** | 15 US metros | Same 15 metros (all are strong AEC markets — heavy construction + development activity) |
| **daily_target** | 50 | 50 (no change) |

**New value proposition:**
> I build managed business development systems for engineering and architecture firms. The system identifies potential clients (developers, property owners, GCs, facilities managers), researches them, writes personalized introductions, and delivers warm conversations — so your principals can focus on winning work and delivering projects, not prospecting. This very email is proof it works.

**New sender bio:**
> Zelu Sottomayor builds managed business development systems for AEC firms. He helps engineering and architecture practices generate a steady pipeline of new client introductions without pulling principals away from billable work or relying solely on referrals and RFPs.

### 2. `config/email_templates.yaml`

Complete rewrite of all 4 emails + personalization instructions. Key changes:

- **Language shift**: "cold email automation" → "business development engine" / "introduction system"
- **Pain point shift**: "feast-or-famine client pipeline" → "over-reliance on referrals and RFPs" / "seller-doers stretched thin"
- **Value shift**: "like hiring a salesperson" → "like having a dedicated BD coordinator filling your pipeline while you focus on delivery"
- **Proof point**: Same — "this email found you, researched you, wrote itself"
- **CTA**: Same — book a call

#### Email 1 — Initial Outreach (Day 0)

Subject options:
- "New project pipeline for {{company_name}}"
- "{{first_name}}, quick question about your BD pipeline"
- "How {{company_name}} could win more private-sector work"

Body:
```
Hi {{first_name}},

{{personalized_opener}}

Here's the thing: this email found {{company_name}}, researched your firm,
wrote itself, and landed in your inbox — all automatically.

That's what I build for engineering and architecture firms: a managed
business development system. It identifies potential clients, writes
personalized introductions, sends them, and follows up — so your
principals can focus on delivery, not prospecting.

Think of it as having a dedicated BD coordinator filling your pipeline,
for a fraction of the cost of a full-time hire.

{{specific_pain_point}}

If you're open to seeing how this would work for {{company_name}},
grab 20 minutes here: https://zelusottomayor.com/book-call

Best,
Ze Lu Sottomayor
```

#### Email 2 — Follow-up (Day 3)

Subject: "Re: New project pipeline for {{company_name}}"

Body:
```
Hi {{first_name}},

Quick follow-up.

Most AEC firms I talk to face the same challenge: 70-80% of work
comes from repeat clients, which is great — until a key client
pauses or a project wraps, and there's nothing behind it.

What I help firms like yours do:

- Identify developers, property owners, GCs, and facilities
  managers who match your ideal project profile
- Send personalized introductions at scale (not generic blasts)
- Follow up automatically so no opportunity falls through the cracks
- Deliver warm conversations — your principals take it from there

Your team focuses on winning work and delivering. The system handles
top-of-funnel.

Worth a conversation?

Best,
Ze Lu Sottomayor
```

#### Email 3 — Value Add (Day 7)

Subject: "Re: New project pipeline for {{company_name}}"

Body:
```
Hi {{first_name}},

One more thought —

{{industry_specific_insight}}

Most firms I work with rely on referrals and RFP boards for
new business. Both work, but neither is proactive — you're
always waiting for the phone to ring.

A BD system that runs in the background means your pipeline
doesn't depend on timing or luck.

Happy to walk you through it: https://zelusottomayor.com/book-call

Best,
Ze Lu Sottomayor
```

#### Email 4 — Breakup (Day 12)

Subject: "Closing the loop"

Body:
```
Hi {{first_name}},

I'll assume the timing isn't right — no worries at all.

If building a more predictable pipeline ever becomes a priority
for {{company_name}}, the door's open.

Cheers,
Ze Lu Sottomayor
```

#### Personalization Instructions (for Claude AI)

```
You are writing personalized cold email components for Zelu Sottomayor,
who builds managed business development systems for AEC firms
(engineering, architecture, environmental consulting).

The core offer: A system that continuously identifies potential clients
(developers, property owners, GCs, facilities managers), researches them,
writes personalized introductions, sends sequences, and delivers warm
conversations — so principals and seller-doers can focus on winning work
and delivering projects instead of prospecting.

Your task is to create highly personalized email components based on
research about the prospect's firm.

Guidelines:
- Be conversational and human, not salesy
- Reference specific things about their firm (services, project types,
  markets served, specializations)
- Focus pain points on BD challenges common to AEC:
  - Over-reliance on repeat clients and referrals
  - Seller-doers stretched between delivery and BD
  - Inconsistent pipeline / feast-or-famine project flow
  - Missing opportunities because nobody is proactively prospecting
  - Spending time on RFPs they don't win instead of building relationships early
- Keep it concise — these are busy technical professionals
- The tone should be professional and direct, not hype-y
- Avoid generic phrases like "I hope this email finds you well"
- Use AEC language: "projects" not "deals", "clients" not "customers",
  "principals" not "executives", "winning work" not "closing sales"

For each lead, generate:
1. personalized_opener: 1-2 sentences referencing something specific about
   their firm (project types, markets, certifications, specializations).
   Max 25 words.
2. specific_pain_point: 1-2 sentences about why a managed BD system would
   help THIS specific firm (based on their size, services, or market).
   Max 25 words.
3. industry_specific_insight: A valuable observation about business
   development in their specific AEC sub-sector. Max 25 words.
4. suggested_subject: A compelling, non-spammy subject line relevant to
   AEC business development. Max 8 words.
```

### 3. `src/outreach/personalize.py`

Two changes:

**a) Lead scoring — `calculate_lead_score()` (line 234)**

Current `good_industries`:
```python
good_industries = ["marketing", "advertising", "media", "communications", "pr", "creative", "digital"]
```

New:
```python
good_industries = ["engineering", "architecture", "environmental", "consulting", "construction", "surveying", "geotechnical", "civil", "structural", "mechanical"]
```

**b) Fallback content (line 110)**

Current fallback references "your agency" — update to "your firm" and AEC-relevant language.

### 4. Instantly Campaign

**Action items:**
1. **Pause** the existing "Agency Outreach" campaign in Instantly (do NOT delete — preserves historical data)
2. **Create** new campaign "AEC Business Development" with:
   - Same sending schedule: Mon-Fri, 9 AM-5 PM EST
   - Same limits: 50 emails/day, 120s delay between sends
   - New 4-email sequence (from templates above)
   - Same sender inbox(es)

The system already looks up campaigns by name (`campaign_name` in settings.yaml), so changing the config value is enough — the code will find the new campaign automatically.

### 5. Google Sheets CRM

**Create a new tab**: "AEC Leads" with the same 28-column schema as "Leads".

The old "Leads" tab stays intact as an archive of the agency campaign data. The `sheet_name` config change points the system at the new tab.

### 6. Apollo Seniority Targeting

Current Apollo search targets: `["owner", "founder", "c_suite", "vp", "director"]`

This is already correct for AEC. The decision-makers at 5-50 person AEC firms are:
- **Principal** / Managing Principal (maps to owner/founder/c_suite)
- **President** / CEO (maps to c_suite)
- **Partner** / Managing Partner (maps to owner/founder)
- **Director of Business Development** (maps to director)
- **VP** (maps to vp)

No code change needed — the seniority filters are correct.

---

## Implementation order

### Step 1: Create new Instantly campaign
- Log into Instantly dashboard
- Pause "Agency Outreach" campaign
- Create "AEC Business Development" campaign
- Set up the 4-email sequence with new templates
- Configure schedule (Mon-Fri, 9-5 EST, 50/day, 120s delay)

### Step 2: Create new Google Sheets tab
- Add "AEC Leads" tab with same column structure
- Keep "Leads" tab as archive

### Step 3: Update config files
- `config/settings.yaml` — all changes listed above
- `config/email_templates.yaml` — full rewrite

### Step 4: Update scoring logic
- `src/outreach/personalize.py` — update `good_industries` + fallback text

### Step 5: Deploy
- Run `./scripts/deploy-automation.sh --sync-config`
- Verify on server: config files updated, .env untouched
- Manual test run to verify end-to-end

### Step 6: Monitor first run
- Watch logs for first daily execution
- Verify: Google Maps returns AEC firms (not agencies)
- Verify: Apollo enriches correctly, finds principals
- Verify: Claude personalization uses AEC language
- Verify: Leads land in Instantly "AEC Business Development" campaign
- Verify: Leads appear in "AEC Leads" sheet tab

---

## Files changed (summary)

| File | Change type | Scope |
|---|---|---|
| `config/settings.yaml` | Edit | Search queries, excludes, campaign name, value prop, bio, sheet name |
| `config/email_templates.yaml` | Rewrite | All 4 emails, personalization instructions |
| `src/outreach/personalize.py` | Edit | 2 small changes (scoring + fallback) |

**Total code changes: ~3 lines of Python + 2 config files.**

The rest of the system (Google Maps client, Apollo client, Instantly client, CRM, sync, Docker, deploy) is untouched.

---

## What we're NOT doing (and why)

- **Not renaming `search_agencies` function** — the function is generic (takes any search query). Renaming is cosmetic and introduces unnecessary diff noise.
- **Not changing Apollo API logic** — seniority targeting already covers AEC decision-makers.
- **Not changing cities** — all 15 current metros have strong AEC presence.
- **Not changing daily_target or sending limits** — 50 leads/day and 50 emails/day is the right starting volume to test the new ICP before scaling.
- **Not touching the Local Services pipeline** — that's a separate phone-first workflow. It stays as-is.
