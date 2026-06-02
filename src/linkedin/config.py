"""
LinkedIn Outbound — Configuration
===================================
Selection criteria, safety limits, DM templates, and CRM schema.
"""

# ---------------------------------------------------------------------------
# Google Sheets CRM — "LinkedIn Outbound" tab
# ---------------------------------------------------------------------------

LINKEDIN_SHEET_NAME = "LinkedIn Outbound"

LINKEDIN_CRM_HEADERS = [
    "ID",
    "Company",
    "Contact Name",
    "Title",
    "LinkedIn URL",
    "Company LinkedIn",
    "Job Hiring",
    "Country",
    "Employee Count",
    "Industry",
    "Status",
    "Connected? (manual)",   # Checkbox — Jose toggles YES when he sees acceptance first
    "Connection Sent",
    "Connection Accepted",
    "DM 1 Variant",          # A, B, or C — tracks which message variant was used
    "DM 1 Sent",
    "DM 2 Sent",
    "DM 3 Sent",
    "Reply",
    "Reply Date",
    "Notes",
    "Source",
    "Description Snippet",   # From Apify — used for Claude personalization
]

# Column index lookup
LCOL = {}
for _i, _h in enumerate(LINKEDIN_CRM_HEADERS):
    _key = _h.lower().replace(" ", "_").replace("?", "").replace("(", "").replace(")", "")
    if _key not in LCOL:
        LCOL[_key] = _i


# ---------------------------------------------------------------------------
# Decision-Maker Selection Criteria (size-dependent)
# ---------------------------------------------------------------------------

SELECTION_CRITERIA = {
    # Under 20 employees → Founder / CEO
    "small": {
        "max_employees": 20,
        "title_searches": [
            ["Founder", "Co-Founder"],
            ["CEO", "Chief Executive Officer"],
            ["Owner", "Managing Director"],
        ],
        "seniority_keywords": ["founder", "ceo", "owner", "managing director"],
    },
    # 20-50 employees → VP/Head of Sales
    "medium": {
        "max_employees": 50,
        "title_searches": [
            ["VP Sales", "VP of Sales", "Vice President Sales"],
            ["Head of Sales", "Head of Growth"],
            ["Sales Director", "Director of Sales"],
            ["CEO", "Founder"],  # fallback
        ],
        "seniority_keywords": ["vp", "head", "director", "sales", "growth"],
    },
    # 50+ employees → CRO / VP Sales
    "large": {
        "max_employees": 999999,
        "title_searches": [
            ["CRO", "Chief Revenue Officer"],
            ["VP Sales", "VP of Sales"],
            ["Director of Sales", "Director of Business Development"],
            ["Head of Revenue", "Head of Sales"],  # fallback
        ],
        "seniority_keywords": ["cro", "chief revenue", "vp", "director", "sales"],
    },
}

# Skip prospect if any of these appear in their headline/title
SKIP_TITLE_KEYWORDS = [
    "hiring", "recruiter", "talent acquisition", "recruiting",
    "hr ", "human resources", "people operations",
    "intern", "assistant", "coordinator",
]

# Validation: skip if they left the company
SKIP_INDICATORS = [
    "formerly at", "ex-", "former",
]


def get_tier(employee_count: int) -> str:
    """Return the selection tier based on company size."""
    if not employee_count or employee_count < 20:
        return "small"
    elif employee_count <= 50:
        return "medium"
    else:
        return "large"


# ---------------------------------------------------------------------------
# Safety Limits
# ---------------------------------------------------------------------------

SAFETY_LIMITS = {
    "max_connections_per_day": 20,
    "max_connections_per_week": 100,
    "max_dms_per_day": 40,
    "delay_between_actions_min": 45,   # seconds
    "delay_between_actions_max": 120,  # seconds
    "business_hours_start": 10,        # 10 AM
    "business_hours_end": 17,          # 5 PM
    "weekdays_only": True,

    # Warm-up ramp
    "warmup_week_1": 10,
    "warmup_week_2": 15,
    "warmup_week_3_plus": 20,

    # DM timing
    "dm2_delay_days": 5,   # Days after DM 1
    "dm3_delay_days": 10,  # Days after DM 2
    "no_reply_close_days": 7,  # Days after DM 3 to mark "No Reply"
}


# ---------------------------------------------------------------------------
# DM Message Templates (Claude personalizes these)
# ---------------------------------------------------------------------------

DM_TEMPLATES = {
    # -----------------------------------------------------------------------
    # DM 1 — THREE VARIANTS for A/B/C testing
    # Assigned round-robin (A → B → C → A → ...) and tracked in CRM.
    # Monitor reply rates per variant to find the winner.
    # -----------------------------------------------------------------------

    "dm1_A": {
        "variant": "A",
        "angle": "direct_pitch",  # Straight to value prop
        "system_prompt": """You write short, direct LinkedIn DMs for a B2B automation agency.
Tone: Direct and confident. No fluff, no "I hope this finds you well."
You always reference the specific hiring signal (the SDR/BDR role they're posting).
Lead with what you do and why it matters to them.
Keep the message under 280 characters.
End with a clear, low-friction call to action.
Do NOT use emojis. Do NOT use bullet points.""",

        "user_prompt": """Write a LinkedIn DM (variant A: direct pitch) to this person:

Name: {first_name}
Title: {title}
Company: {company}
Industry context: {industry}
They are hiring: {job_hiring}
Country: {country}
Company description: {description_snippet}

The message should:
1. Address them by first name
2. State that you build automated outbound systems
3. Reference the specific role they're hiring
4. Offer to show how it works in 15 min
5. Stay under 280 characters

Example tone: "{first_name}, I build automated outbound systems for [industry] companies. Saw {company} is hiring [role] — I help teams get the same pipeline without the headcount. Happy to show you how it works." """,
    },

    "dm1_B": {
        "variant": "B",
        "angle": "result_lead",  # Lead with a specific result/number
        "system_prompt": """You write short, direct LinkedIn DMs for a B2B automation agency.
Tone: Direct and confident. Lead with a concrete result or number.
Reference the hiring signal but focus on what they could achieve instead.
Keep the message under 280 characters.
End with a clear, low-friction call to action.
Do NOT use emojis. Do NOT use bullet points.""",

        "user_prompt": """Write a LinkedIn DM (variant B: result-led) to this person:

Name: {first_name}
Title: {title}
Company: {company}
Industry context: {industry}
They are hiring: {job_hiring}
Country: {country}
Company description: {description_snippet}

The message should:
1. Address them by first name
2. Lead with a concrete result (e.g., "15+ qualified meetings/month" or "pipeline equivalent to 2 SDRs")
3. Briefly mention you noticed they're hiring for the role
4. Ask if a quick walkthrough would be useful
5. Stay under 280 characters

Example tone: "{first_name}, we helped a [industry] company book 15+ meetings/month with zero SDRs. Noticed {company} is hiring [role] — happy to show you what the alternative looks like." """,
    },

    "dm1_C": {
        "variant": "C",
        "angle": "challenge_assumption",  # Challenge the SDR hire approach
        "system_prompt": """You write short, direct LinkedIn DMs for a B2B automation agency.
Tone: Direct and confident. Challenge their current approach (hiring SDRs) with a better alternative.
Be provocative but respectful — make them think.
Keep the message under 280 characters.
End with a clear, low-friction call to action.
Do NOT use emojis. Do NOT use bullet points.""",

        "user_prompt": """Write a LinkedIn DM (variant C: challenge the assumption) to this person:

Name: {first_name}
Title: {title}
Company: {company}
Industry context: {industry}
They are hiring: {job_hiring}
Country: {country}
Company description: {description_snippet}

The message should:
1. Address them by first name
2. Reference that they're hiring the specific role
3. Challenge the assumption that hiring SDRs is the best way to build pipeline
4. Present automation as the alternative without being preachy
5. Stay under 280 characters

Example tone: "{first_name}, hiring SDRs is one way to build pipeline — but what if {company} could get the same output without the ramp time and turnover? That's what we build. Worth 15 min?" """,
    },

    "dm2": {
        "system_prompt": """You write short, direct LinkedIn follow-up DMs for a B2B automation agency.
Tone: Direct and confident. This is a follow-up — be brief.
Reference a specific result (meetings booked, pipeline generated).
Keep under 250 characters. End with a yes/no question.""",

        "user_prompt": """Write a LinkedIn follow-up DM (message 2 of 3, sent 5 days after message 1 with no reply):

Name: {first_name}
Company: {company}
Industry: {industry}
They are hiring: {job_hiring}

The message should:
1. Be a quick follow-up (no re-introduction)
2. Mention a specific result (e.g., "15+ meetings/month" or similar)
3. Relate to their industry if possible
4. Ask if a 15-minute walkthrough is worth their time
5. Stay under 250 characters""",
    },

    "dm3": {
        "system_prompt": """You write short, no-pressure LinkedIn closing DMs.
Tone: Direct but gracious. This is the last message — leave the door open.
Keep under 200 characters. No hard sell.""",

        "user_prompt": """Write a final LinkedIn DM (message 3 of 3, sent 10 days after message 2 with no reply):

Name: {first_name}
Company: {company}
Job they're hiring: {job_hiring}

The message should:
1. Acknowledge this is the last message
2. Leave the door open for future
3. Wish them luck with the hire
4. Stay under 200 characters""",
    },
}

# ---------------------------------------------------------------------------
# DM 1 Variant Rotation (A/B/C testing)
# ---------------------------------------------------------------------------

DM1_VARIANTS = ["A", "B", "C"]
DM1_VARIANT_KEYS = [f"dm1_{v}" for v in DM1_VARIANTS]


def get_next_dm1_variant(variants_used: dict) -> str:
    """Pick the next DM1 variant using round-robin to keep counts balanced.

    Args:
        variants_used: dict mapping variant letter to count, e.g. {"A": 12, "B": 11, "C": 12}

    Returns:
        The variant letter ("A", "B", or "C") with the lowest count.
    """
    counts = {v: variants_used.get(v, 0) for v in DM1_VARIANTS}
    # Pick the variant with the fewest sends (ties go to A first)
    return min(DM1_VARIANTS, key=lambda v: counts[v])


def get_dm1_template(variant: str) -> dict:
    """Get the DM template for a specific variant."""
    key = f"dm1_{variant}"
    return DM_TEMPLATES.get(key, DM_TEMPLATES["dm1_A"])
