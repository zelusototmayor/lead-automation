# Monday 2026-04-20 v2 Restart — Handoff for Claude Code

**Status:** all code work is done and committed. What's left is 4 API/UI
actions that need network access to Instantly + Google Sheets. This doc
gives you everything you need to finish them from the lead-automation
project.

**Hard rules — do not violate:**

1. **All 4 new Instantly campaigns MUST stay PAUSED** after creation.
   Ze flips them live himself Monday morning. Never call
   `/campaigns/{id}/activate` in this workflow.
2. **No product URLs anywhere in email bodies** — no `calendly`, no
   `/book-call`, no `/demo`, no `zelusottomayor.com` links. The ONLY
   link-like reference allowed is the bare email `max@zelusottomayor.com`
   used as a try-it address for the Logistics offer.
3. **Do not delete, rename, or archive the old Google Sheets tabs**
   (`AEC Leads`, `B2B Startups`, `EU B2B Leads`). Ze wants them alongside
   the v2 tabs for comparison.
4. **Do not send any test emails.** Dry runs only use `--source-only`.

---

## Context — what's already done

- Apollo is fully dropped at runtime. `src/lead_sourcing/apollo.py` is
  left dormant on disk (backward compat), but all 3 existing pipelines
  (`main.py` AEC, `startups.py`, `eu_outreach.py`) now source contacts
  via `InstantlyEnrichmentClient` (SuperSearch).
- New pipeline `src/logistics.py` added for UK+Spain SMB freight
  brokers / forwarders / 3PLs.
- `config/settings.yaml` updated with v2 sheet names, new campaign
  names, SuperSearch credit budgets, and a full `logistics` section.
- `config/email_templates.yaml` (AEC) was tightened: plain-text, no
  URLs, reply-based CTAs, bodies under ~120 words.
- `scripts/requeue_polluted_to_v2.py` is ready — copies polluted-window
  leads into v2 tabs with clean status.
- README rewritten to reflect the 4-campaign structure.

Verified: all Python files `py_compile`-clean, settings.yaml
`yaml.safe_load`-clean, zero `https://` URLs remain in
`config/email_templates.yaml`.

---

## Tools / config you'll use

- **Instantly V2 API** — `InstantlyClient` in `src/outreach/instantly_client.py`.
  API key: `INSTANTLY_API_KEY` (already in `.env`).
  Base URL: `https://api.instantly.ai/api/v2`.
- **Google Sheets** — `GoogleSheetsCRM` in `src/crm/sheets.py`.
  Service account creds: `config/google_credentials.json`.
  Spreadsheet ID: `1ZdhkP_Hq-340eVEOS-RKwHGjDaX0vNVP6vO48XzkOx8`.
  Note: `GoogleSheetsCRM.__init__` auto-creates the tab if missing
  and writes `CRM_HEADERS` into row 1.
- **All campaign / sheet names** — pull from `config/settings.yaml`.
  Do not hardcode names in scripts.

---

## Action 1 — Create the 4 v2 Google Sheets tabs

Spreadsheet ID: `1ZdhkP_Hq-340eVEOS-RKwHGjDaX0vNVP6vO48XzkOx8`.

Tabs to create (if they don't already exist):

- `AEC Leads v2`
- `B2B Startups v2`
- `EU B2B Leads v2`
- `Logistics Quote v2`

**Easiest path:** instantiate `GoogleSheetsCRM` once per tab — the
constructor auto-creates the tab with the full `CRM_HEADERS` schema:

```python
# scripts/create_v2_tabs.py — run once
from src.crm.sheets import GoogleSheetsCRM

SPREADSHEET_ID = "1ZdhkP_Hq-340eVEOS-RKwHGjDaX0vNVP6vO48XzkOx8"
CREDS = "config/google_credentials.json"

for tab in ["AEC Leads v2", "B2B Startups v2",
            "EU B2B Leads v2", "Logistics Quote v2"]:
    GoogleSheetsCRM(credentials_file=CREDS,
                    spreadsheet_id=SPREADSHEET_ID,
                    sheet_name=tab)
    print(f"Ready: {tab}")
```

Leave old tabs (`AEC Leads`, `B2B Startups`, `EU B2B Leads`) untouched.
There is no old `Logistics Quote` — this is a net-new tab.

---

## Action 2 — Instantly: archive old, create 4 new (PAUSED)

### 2a. List current campaigns first

Don't trust hardcoded IDs. Start with:

```python
from src.outreach.instantly_client import InstantlyClient
import os

client = InstantlyClient(os.environ["INSTANTLY_API_KEY"])
for c in client.list_campaigns():
    print(c.get("id"), c.get("status"), repr(c.get("name")))
```

You're looking for 3 old campaigns. They likely appear as:

- `B2B Startups Outbound (copy)` or similar
- `AEC Business Development (copy)` or similar
- `EU B2B - hiring sales` or similar

If name matching is ambiguous, ask Ze before renaming.

### 2b. Archive the 3 old campaigns

For each of the 3 matched old campaigns:

1. Pause (if not already):
   `POST /api/v2/campaigns/{id}/pause` — use `client.pause_campaign(id)`.
2. Rename with `[ARCHIVED]` prefix:
   `PATCH /api/v2/campaigns/{id}` with body `{"name": "[ARCHIVED] <old name>"}`.

`InstantlyClient` doesn't have a rename helper yet — use `_make_request`
directly:

```python
client._make_request("PATCH", f"campaigns/{id}",
                     data={"name": f"[ARCHIVED] {old_name}"})
```

### 2c. Create 4 new campaigns

Pull all of this from `config/settings.yaml` — do not hardcode.

| Campaign name (settings.yaml key)      | Inbox (sending account)        |
|----------------------------------------|--------------------------------|
| `AEC — Outbound System`                | `ana@zeluautomations.com`      |
| `Startups — Outbound System`           | `jose@zeluautomations.com`     |
| `EU B2B — Outbound System`             | `ana@zelusotto.com`            |
| `Logistics — Quote Agent`              | `jose@zelusotto.com`           |

**For each of the 4 campaigns, configure:**

1. Create the campaign (starts paused by default):
   `client.create_campaign(name)` → returns `{id, ...}`.
2. Assign the sending account (only the one inbox above — each campaign
   owns exactly ONE inbox). Via API:
   ```
   PATCH /api/v2/campaigns/{id}
   { "email_list": ["ana@zeluautomations.com"] }
   ```
3. Set schedule: Mon–Fri 9:00–17:00, appropriate timezone per campaign.
   Use `client.set_campaign_schedule(...)`:
   - AEC, Startups: `America/New_York`
   - EU B2B, Logistics: `Europe/London`
4. Add the 4-email sequence (see Action 2d below).
5. Subject variants: take the 3 `subject_variants` from the matching
   settings.yaml section. Subject variants are applied to **Email 1
   only**; Email 2/3/4 use `Re: <variant>`.
6. Sending limits: 15/day for AEC + Startups + Logistics, 12/day for EU.
   (Those match `daily_target` per campaign.)
7. **Verify campaign ends up in `paused` status. If not, call
   `client.pause_campaign(id)` immediately.**

### 2d. Email sequences per campaign

#### AEC — Outbound System

The full 4-email sequence is in `config/email_templates.yaml` under
`sequences.default`. Push it verbatim. Placeholders like
`{{first_name}}`, `{{company_name}}`, `{{personalized_opener}}`,
`{{specific_pain_point}}`, `{{industry_specific_insight}}` are filled
per-lead by `src/outreach/personalize.py` before upload (via
`add_leads_to_campaign`). The Instantly sequence itself should still
have the email bodies as templates so Instantly's variable
substitution renders per-lead payloads cleanly.

#### Startups — Outbound System

No fixed YAML — `src/startups.py` + `personalize.py` generate the
full personalized body per-lead and upload. So in Instantly, set up
the sequence structure (4 steps, day gaps 0/3/7/12, subject variants)
but leave email bodies as minimal placeholders like:

```
{{body}}
```

with a Claude-generated body passed in per-lead. If Instantly doesn't
accept an empty-ish template, use a one-line safety fallback:

```
Hi {{firstName}},

{{body}}

Best,
Ze Lu
```

Day gaps: 0, 3, 7, 12.

#### EU B2B — Outbound System

Same pattern as Startups — body is per-lead Claude-generated via
`src/eu_outreach.py`. Same 4-step cadence (0/3/7/12). Subject
variants from `eu_outreach.subject_variants` in settings.yaml.

#### Logistics — Quote Agent

**This campaign's copy lives IN Instantly, not in the repo.** Ze wants
the 4 emails built directly inside the campaign. Use the drafts below
— plain text, no URLs, `max@zelusottomayor.com` as try-it. Use
Instantly's variable syntax (`{{firstName}}`, `{{companyName}}`).

**Schedule:** Mon–Fri 9:00–17:00 Europe/London.
**Day gaps:** Email 1 @ day 0, Email 2 @ day 3, Email 3 @ day 7,
Email 4 @ day 12.

**Subject line variants** (apply to Email 1 only; 2/3/4 use "Re: …"):

- `Quick q about {{companyName}}'s quote flow`
- `{{firstName}}, on your freight quotes`
- `Faster freight quotes for {{companyName}}`

**Email 1 body:**

```
Hi {{firstName}},

Noticed {{companyName}} works SMB freight — broker / forwarder space is
brutal right now with shippers wanting turnarounds in hours, not days.

I built an agent that reads an inbound quote request, pulls rates from
your carriers, and drafts the reply email — so your team turns around
quotes in minutes instead of hours.

Want to see it work on one of your own requests? I can set up a try-it
for {{companyName}} — no integration, just forward a quote email to
max@zelusottomayor.com and I'll send back what the agent produced.

Worth 15 min?

Best,
Ze Lu
```

**Email 2 body** (Day 3 — subject: `Re: <same as email 1 subject>`):

```
Hi {{firstName}},

Nudging on the note below.

Most small freight brokers I talk to lose deals not on price but on
response time — shipper sends out 5 RFQs at 9am, books with whoever
replies first. The agent closes that gap.

If the "forward a quote to max@zelusottomayor.com" try-it sounds useful,
just reply "send it" and I'll walk you through.

Best,
Ze Lu
```

**Email 3 body** (Day 7 — subject: `Re: …`):

```
Hi {{firstName}},

One more —

The agent handles the annoying parts: matching lane + equipment, pulling
the right carrier rate, writing the reply in your tone. Your team still
reviews before sending.

Typical fit: 3–15 people, tired of opening spreadsheets at 7am for
next-day quotes.

Worth a quick look?

Best,
Ze Lu
```

**Email 4 body** (Day 12 — subject: `Closing loop on {{companyName}}`):

```
Hi {{firstName}},

I'll assume quote speed isn't a priority right now — no worries.

If it ever becomes one, reply to this thread and I'll pick it back up.

Cheers,
Ze Lu
```

**Note on Spanish leads:** `src/logistics.py` tags Spain leads with
`language=es`, but for Monday launch the sequence is English-only — UK
and Spain directors typically handle English B2B. A Spanish variant
campaign can be spun up later if reply rates lag in ES.

---

## Action 3 — Run the polluted-window re-queue

Precondition: Action 1 is done (v2 tabs exist).

```bash
# 1. Dry-run first. Report the counts per campaign.
python scripts/requeue_polluted_to_v2.py --dry-run

# 2. If counts look sane (small-to-medium per campaign, no crazy volume),
#    run for real.
python scripts/requeue_polluted_to_v2.py
```

Expected output: per-campaign counts for scanned / polluted / requeued
/ dup-in-v2. Report the numbers to Ze before continuing to Action 4.

Default polluted window is `2026-03-01 → 2026-04-18`, defined at the
top of the script. If Ze wants a different window, use
`--since YYYY-MM-DD --until YYYY-MM-DD`.

The old tabs are NEVER modified by this script — they stay as-is.

---

## Action 4 — Source-only dry runs (verify v2 wiring)

Goal: confirm every pipeline sources + enriches into its v2 tab. No
personalization, no Instantly pushes, no emails sent.

```bash
# Startups / EU / Logistics all support --source-only --target
python src/startups.py    --target 3 --source-only
python src/eu_outreach.py --target 3 --source-only
python src/logistics.py   --target 3 --source-only
```

AEC (`src/main.py`) doesn't currently have a `--source-only` flag —
it's a full workflow. For a sourcing-only check, run inline:

```bash
python -c "
from pathlib import Path
from src.main import LeadAutomation, load_config, load_email_templates
cfg_dir = Path('config')
cfg = load_config(str(cfg_dir / 'settings.yaml'))
tpl = load_email_templates(str(cfg_dir / 'email_templates.yaml'))
auto = LeadAutomation(cfg, tpl)
leads = auto.run_daily_sourcing(target_leads=3)
print('AEC sourcing OK — leads:', len(leads))
print('SuperSearch credits used:', auto.enrichment_client.get_credit_summary())
"
```

**Verify after each run:**

- The v2 tab for that campaign gained ~3 rows with fresh
  `Status = New`.
- `SuperSearch credits used` ≤ ~15 total across all 4 dry runs.
- No errors mentioning `Apollo` — Apollo must not be called at
  runtime anywhere.
- No rows written to old tabs.

**IMPORTANT:** the 4 Instantly campaigns must still be PAUSED when
these dry runs complete. Source-only skips the Instantly push, so
nothing should end up queued in Instantly — but double-check campaign
status afterward anyway.

---

## Final check before handing back

- [ ] All 4 v2 Sheets tabs exist with correct headers.
- [ ] 3 old Instantly campaigns: paused + renamed with `[ARCHIVED]`.
- [ ] 4 new Instantly campaigns exist, each paired to exactly ONE
      dedicated inbox (per the table in 2c), with Mon–Fri 9–5 schedule,
      3 subject variants on Email 1, 4-step sequence with 0/3/7/12 day
      gaps — **and all 4 are PAUSED**.
- [ ] Logistics email bodies contain zero `https://` URLs and
      reference only `max@zelusottomayor.com` as the try-it address.
- [ ] `scripts/requeue_polluted_to_v2.py` ran successfully (write
      mode), counts reported.
- [ ] All 4 source-only dry runs wrote ~3 leads each to the correct
      v2 tab.
- [ ] No errors referencing Apollo at runtime.

Report back a short summary (counts, campaign IDs of the 4 new
campaigns, any surprises). Ze resumes Monday morning by flipping the
paused campaigns live in the Instantly UI.
