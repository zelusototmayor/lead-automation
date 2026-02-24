# Multi-ICP Outbound Automation Analysis

## Current System (Your Agency Automation)

**Single Campaign Architecture:**
- 1 ICP: US marketing/PR agencies
- 1 set of cities (US metros)
- 1 set of search queries
- 1 value proposition
- 1 email sequence
- 1 Instantly campaign
- 1 Google Sheet CRM
- Daily target: 11 leads total

---

## What Your Boss's Company Would Need

### **The Big Picture: 4 Parallel Outbound Cycles**

You'd essentially be running **4 separate automations** under one codebase. Think of it as 4 different products/services they're selling.

### **ICP Breakdown & Structural Needs**

#### **ICP 1: Restaurants & Bars (Lisbon, Porto, Algarve)**
- **Cities**: 3 specific Portuguese cities
- **Search queries**: "restaurant", "bar", "gastropub", "wine bar", "cocktail bar", etc.
- **Lead sourcing**: Google Maps (same as current)
- **Enrichment**: Apollo might struggle here—most restaurants won't have LinkedIn decision-makers. You'd likely rely more on Google Maps data (phone, basic website)
- **Value prop**: Whatever they're selling to restaurants (POS system? Booking software? Supplies?)
- **Email sequence**: Restaurant-specific messaging
- **Instantly campaign**: "Restaurant Outreach PT"
- **CRM sheet**: Either separate tab or separate sheet

#### **ICP 2: Co-working Spaces & Hostels**
- **Cities**: Likely broader (Lisbon, Porto, maybe other EU cities?)
- **Search queries**: "coworking space", "hostel", "shared office", "business center"
- **Lead sourcing**: Google Maps
- **Enrichment**: Apollo should work better here—these businesses are more "corporate"
- **Value prop**: Different from restaurants
- **Email sequence**: Co-working specific
- **Instantly campaign**: "Coworking Outreach"
- **CRM**: Separate tracking

#### **ICP 3: National Companies (Mid-Large, Portugal)**
- **Cities**: Not city-based—national search
- **Search queries**: This is fundamentally different. You can't use Google Maps the same way. You'd need:
  - Apollo search by company size + Portugal location
  - Or LinkedIn Sales Navigator scraping
  - Or company databases (Crunchbase, Portuguese business registries)
- **Lead sourcing**: **MAJOR DIFFERENCE**—not Google Maps, need different data source
- **Enrichment**: Apollo would be primary source
- **Value prop**: Enterprise-focused messaging
- **Email sequence**: Corporate outreach
- **Instantly campaign**: "Enterprise PT"
- **CRM**: Separate tracking

#### **ICP 4: Events/Festivals/Catering**
- **Cities**: Likely national or specific regions
- **Search queries**: "music festival", "event organizer", "catering company", "event production"
- **Lead sourcing**: Mix of Google Maps + manual lists (festivals are often not in Google Maps)
- **Enrichment**: Apollo + manual research
- **Value prop**: Event-specific
- **Email sequence**: Events industry messaging
- **Instantly campaign**: "Events Outreach PT"
- **CRM**: Separate tracking

---

## **Key Structural Differences from Your Current Setup**

### 1. **Configuration Complexity**
**Current**: 1 config file, 1 set of parameters
**New**: Would need:
- Config file that supports multiple "campaigns" or "clients"
- Each campaign has its own:
  - Target cities
  - Search queries
  - Value proposition
  - Email templates
  - Instantly campaign name
  - Daily lead targets (split across 4 ICPs)

### 2. **CRM Structure**
**Current**: Single Google Sheet with one "Leads" tab
**New**: Either:
- **Option A**: Same sheet, 4 tabs (one per ICP)
- **Option B**: 4 separate sheets
- **Option C**: Same tab but with "ICP" column to filter

**Recommendation**: Option A (4 tabs in same sheet) is cleanest—keeps everything organized but in one place.

### 3. **Lead Sourcing Challenges**

**Major constraint**: ICP #3 (National mid-large companies) **cannot use Google Maps** effectively.

**Why**: Google Maps is city/location-based. To find "mid to large national companies in Portugal," you need:
- Company size filter (# employees, revenue)
- Industry filters
- National scope, not city-specific

**Solution**:
- Use Apollo's company search (not contact enrichment) for ICP #3
- Or integrate LinkedIn Sales Navigator
- Or use Portuguese business databases

This would require **adding a new lead sourcing module** beyond your current Google Maps approach.

### 4. **Daily Lead Distribution**
**Current**: 11 leads/day from 1 ICP
**New**: How many leads per ICP?
- If still 11/day total: ~3 per ICP (uneven distribution)
- If 11/day per ICP: 44 leads/day total (4x current volume)

**Constraint**: API rate limits
- Google Maps: 500 requests/day (fine for 44 leads)
- Apollo: Depends on plan, could be bottleneck
- Instantly: Depends on email account limits

### 5. **Email Personalization**
**Current**: 1 value prop, 1 email sequence
**New**: 4 completely different:
- Value propositions
- Email sequences (4 separate email_templates.yaml sections)
- Personalization prompts (restaurant owner vs. corporate executive = different language)

**Claude AI cost**: 4x personalization calls (but tokens are cheap, so not a big deal)

### 6. **Instantly Campaigns**
**Current**: 1 campaign
**New**: 4 campaigns
- Each with its own email accounts (ideally)
- Separate tracking
- Different sending schedules possibly (restaurants might respond better to different times than corporate)

### 7. **Data Quality Variance**

This is a **critical constraint**:

| ICP | Data Quality | Lead Sourcing Ease |
|-----|-------------|-------------------|
| Restaurants/Bars | Low (often no website, generic emails) | Easy (Google Maps) |
| Co-working/Hostels | Medium | Easy (Google Maps) |
| National Companies | High | **Hard** (need new data source) |
| Events/Festivals | Low-Medium (festivals often poorly indexed) | Medium-Hard |

**Restaurants** will have the **lowest data quality**:
- Many won't have websites
- Email will be generic (info@restaurant.com)
- No LinkedIn presence
- Phone numbers might be best contact method

**National companies** will have **best data quality** but **hardest sourcing**.

---

## **How Many "Cycles" Do You Need?**

**Answer**: You need **4 independent outbound cycles** running in parallel.

Each cycle is:
1. **Lead Sourcing** → 2. **Enrichment** → 3. **Personalization** → 4. **Email Sending** → 5. **Follow-up**

But they share the same:
- Codebase
- Infrastructure
- Claude AI personalization engine
- Instantly account (but different campaigns)
- Google Sheets (but different tabs)

Think of it like running 4 different businesses through the same assembly line.

---

## **What Would Need to Change in the Code?**

Without making changes, here's what would need to be different:

### **High-Level Architecture Changes:**

1. **Multi-campaign config structure**
   ```yaml
   campaigns:
     - name: "Restaurants PT"
       icp: "restaurants_bars"
       target_cities: [Lisbon, Porto, Algarve]
       search_queries: [...]
       daily_target: 3

     - name: "Coworking PT"
       icp: "coworking_hostels"
       ...
   ```

2. **New lead sourcing module** for company databases (ICP #3)

3. **CRM multi-tab support** or campaign filtering

4. **Multiple email template sets** in email_templates.yaml

5. **Campaign-specific value props** and personalization instructions

6. **Main workflow loop** that iterates through all 4 campaigns

---

## **Constraints & Risks**

### **Technical Constraints:**
1. **Apollo rate limits**: Might need higher tier plan for 4x volume
2. **Google Maps API**: Should be fine, but costs scale with requests
3. **Instantly email sending limits**: Need multiple email accounts (1-2 per ICP) to avoid spam
4. **Lead quality**: Restaurants/events will have lower email deliverability

### **Operational Constraints:**
1. **Data availability**: Not all ICPs are equally "scrapable"
2. **Message-market fit**: Need to deeply understand what they're selling to each ICP
3. **Maintenance**: 4x campaigns = 4x monitoring, optimization, troubleshooting
4. **Setup time**: Need to:
   - Research search queries for each ICP
   - Write 4 different email sequences
   - Define 4 value propositions
   - Set up 4 Instantly campaigns
   - Configure data sources

### **Business Constraints:**
1. **What are they actually selling?** The automation can't work without knowing the product/service for each ICP
2. **Do they have different products for each ICP, or one product for all?**
   - If same product → easier (just different messaging)
   - If different products → much more complex

---

## **Recommendation: Start with 1-2 ICPs**

If I were you, I'd:

1. **Start with the easiest 2 ICPs first**:
   - **Co-working/Hostels** (good data quality, clear use case, Google Maps works)
   - **Restaurants** (easy to find, but prepare for lower response rates due to data quality)

2. **Then add**:
   - **Events/Festivals** (once you figure out sourcing strategy)
   - **National Companies** (requires new data source—biggest lift)

3. **Test with low volume first**: 2-3 leads/day per ICP to validate before scaling

---

## **Bottom Line**

**Feasibility**: Totally doable, but it's **4x the complexity** of your current setup.

**Biggest technical challenge**: ICP #3 (national companies) needs a different lead sourcing approach than Google Maps.

**Biggest operational challenge**: Maintaining 4 separate campaigns with different messaging, data quality, and response patterns.

**Estimated setup effort**:
- Code adaptation: 2-3 days (multi-campaign architecture)
- New data source for ICP #3: 1-2 days
- Writing email sequences: 1 day per ICP (4 days)
- Testing & debugging: 1-2 weeks

**Would it work?** Yes, absolutely. But you'd essentially be building 4 mini-versions of your current automation, not 1 big one.
