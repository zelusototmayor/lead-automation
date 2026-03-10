"""
Email Personalization with Claude AI
=====================================
Generates personalized email content using Claude.
"""

import anthropic
from typing import Optional
import structlog
import yaml

logger = structlog.get_logger()


def detect_aec_vertical(lead: dict, verticals: dict = None) -> tuple[str, dict]:
    """Detect AEC vertical from lead data and return (vertical_name, vertical_config).

    Args:
        lead: Lead data dictionary
        verticals: Optional dict of vertical configs from email_templates.yaml.
                   If None, uses built-in defaults.

    Returns:
        Tuple of (vertical_name, vertical_config_dict)
    """
    defaults = {
        "civil_site": {
            "match_keywords": ["civil", "site", "land development", "grading", "stormwater", "transportation"],
            "buyer": "developers, general contractors, municipalities",
            "pain": "missing developer projects before they hit public RFP",
            "language": "projects, site plans, entitlements, land development",
        },
        "environmental": {
            "match_keywords": ["environmental", "remediation", "phase i", "phase ii", "compliance", "ehs", "hazardous"],
            "buyer": "real estate developers, lenders, attorneys, property owners",
            "pain": "waiting for Phase I calls instead of proactively reaching every developer and lender in your market",
            "language": "due diligence, Phase I/II, site assessments, compliance",
        },
        "geotechnical": {
            "match_keywords": ["geotech", "geotechnical", "subsurface", "soil", "foundation", "drilling"],
            "buyer": "developers, general contractors, civil engineers, architects",
            "pain": "relying on the same 3-4 firms sending you work instead of being the first call on new developments",
            "language": "geotech reports, subsurface investigation, foundation design",
        },
        "architecture": {
            "match_keywords": ["architect", "architecture", "design", "planning", "multifamily"],
            "buyer": "developers, property owners, tenant improvement clients",
            "pain": "waiting on referrals instead of getting in front of developers early",
            "language": "design, entitlements, project delivery, schematic design",
        },
        "general_engineering": {
            "match_keywords": [],
            "buyer": "developers, property owners, general contractors",
            "pain": "principals splitting time between project delivery and chasing new work",
            "language": "projects, clients, winning work, pipeline",
        },
    }

    verts = verticals or defaults
    text = " ".join([
        (lead.get("industry") or ""),
        (lead.get("description") or ""),
        " ".join(lead.get("keywords") or []),
        (lead.get("company") or ""),
    ]).lower()

    for name, cfg in verts.items():
        keywords = cfg.get("match_keywords", [])
        if keywords and any(kw in text for kw in keywords):
            return name, cfg

    return "general_engineering", verts.get("general_engineering", defaults["general_engineering"])


class EmailPersonalizer:
    """Generates personalized email content using Claude AI."""

    def __init__(self, api_key: str, model: str = "claude-sonnet-4-20250514"):
        """
        Initialize the personalizer.

        Args:
            api_key: Anthropic API key
            model: Claude model to use
        """
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model

    def personalize_email(
        self,
        lead: dict,
        template: str,
        sender_info: dict
    ) -> dict:
        """
        Generate personalized email components for a lead.

        Args:
            lead: Lead data dictionary
            template: Email template with placeholders
            sender_info: Information about the sender

        Returns:
            Dictionary with personalized components
        """
        # Build context about the lead
        lead_context = self._build_lead_context(lead)

        # Detect AEC vertical for targeted messaging
        vertical_name, vertical_cfg = detect_aec_vertical(
            lead, sender_info.get("aec_verticals")
        )
        vertical_block = (
            f"Vertical: {vertical_name}\n"
            f"Typical buyers: {vertical_cfg.get('buyer', 'developers, property owners, GCs')}\n"
            f"Key pain point: {vertical_cfg.get('pain', 'principals stretched between delivery and BD')}\n"
            f"Industry language: {vertical_cfg.get('language', 'projects, clients, winning work')}"
        )

        logger.info("AEC vertical detected", company=lead.get("company"), vertical=vertical_name)

        prompt = f"""You are writing personalized cold email snippets. These get inserted into a short email template — every word must earn its place.

SENDER: {sender_info.get('bio', 'Builds managed business development systems for professional services firms.')}

OFFER: {sender_info.get('value_proposition', 'A managed outbound system that fills your pipeline so you can focus on delivery.')}

LEAD:
{lead_context}

VERTICAL:
{vertical_block}

Generate a JSON object with these fields:

1. "personalized_opener": 1-3 sentences referencing something CONCRETE about this company — their specific services, project types, markets, team size, tech stack, or hiring activity. Max 40 words. This is the first thing the reader sees — it must feel researched, not templated.

2. "specific_pain_point": 1-2 sentences about why a managed outbound system helps THIS specific firm. Anchor in the vertical pain point above and mention their specific buyer type. Max 40 words.

3. "industry_specific_insight": A non-obvious observation about business development in their sub-sector. Something a peer would say, not a salesperson. Max 30 words.

4. "suggested_subject": A subject line specific to THIS company. Must not work for any other company. Max 8 words. Reference their vertical, project type, or signal — not generic patterns.

STRICT RULES — violating these makes the email feel like spam:
- NEVER use: "I came across", "I was impressed by", "I noticed your company", "As a company that", "It's clear that", "In today's competitive landscape", "With the current market"
- NEVER start with "Your" or "I" — vary sentence openings
- Reference CONCRETE details from the lead data above — if data is thin, keep it brief and real rather than padding with generic filler
- Use the vertical-specific language and buyer types — do not be generic
- If a Signal is provided (hiring, scaling sales), weave it into the opener AND subject — it's WHY we're reaching out
- Be conversational and direct, like one business owner talking to another

Return ONLY valid JSON, no other text."""

        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=350,
                messages=[{"role": "user", "content": prompt}]
            )

            # Parse the response
            content = response.content[0].text.strip()

            # Clean up JSON if needed
            if content.startswith("```json"):
                content = content[7:]
            if content.startswith("```"):
                content = content[3:]
            if content.endswith("```"):
                content = content[:-3]

            import json
            result = json.loads(content)

            logger.info(
                "Email personalized",
                company=lead.get("company"),
                has_opener=bool(result.get("personalized_opener"))
            )
            return result

        except Exception as e:
            logger.error("Personalization failed", error=str(e))
            # Return minimal fallback — better to be brief than generic
            company = lead.get("company", "your firm")
            industry = lead.get("industry", "")
            city = lead.get("city", "")
            location_bit = f" in {city}" if city else ""
            industry_bit = f" {industry}" if industry else ""
            return {
                "personalized_opener": f"{company}'s{industry_bit} work{location_bit} caught my attention.",
                "specific_pain_point": "Most firms this size have leaders splitting time between delivery and chasing new business — that's the gap we fill.",
                "industry_specific_insight": "Proactive outreach consistently outperforms waiting on referrals and RFPs.",
                "suggested_subject": f"Quick thought for {company}"
            }

    def generate_full_email(
        self,
        lead: dict,
        template: str,
        sender_info: dict
    ) -> dict:
        """
        Generate a complete personalized email.

        Args:
            lead: Lead data dictionary
            template: Email template with placeholders
            sender_info: Information about the sender

        Returns:
            Dictionary with subject and body
        """
        # Get personalized components
        components = self.personalize_email(lead, template, sender_info)

        # Fill in the template
        body = template

        # Replace placeholders
        replacements = {
            "{{first_name}}": lead.get("contact_name", "").split()[0] if lead.get("contact_name") else "there",
            "{{company_name}}": lead.get("company", "your company"),
            "{{company}}": lead.get("company", "your company"),
            "{{industry}}": lead.get("industry", "your industry"),
            "{{city}}": lead.get("city", ""),
            "{{personalized_opener}}": components.get("personalized_opener", ""),
            "{{specific_pain_point}}": components.get("specific_pain_point", ""),
            "{{industry_specific_insight}}": components.get("industry_specific_insight", "")
        }

        for placeholder, value in replacements.items():
            body = body.replace(placeholder, value)

        return {
            "subject": components.get("suggested_subject", f"Quick question for {lead.get('company', 'you')}"),
            "body": body,
            "components": components
        }

    def _build_lead_context(self, lead: dict) -> str:
        """Build a context string about the lead for the AI."""
        parts = []

        if lead.get("company"):
            parts.append(f"Company: {lead['company']}")

        if lead.get("contact_name"):
            parts.append(f"Contact: {lead['contact_name']}")
            if lead.get("title"):
                parts[-1] += f" ({lead['title']})"

        if lead.get("industry"):
            parts.append(f"Industry: {lead['industry']}")

        if lead.get("employee_count"):
            parts.append(f"Company size: ~{lead['employee_count']} employees")

        if lead.get("city") and lead.get("country"):
            parts.append(f"Location: {lead['city']}, {lead['country']}")

        if lead.get("website"):
            parts.append(f"Website: {lead['website']}")

        if lead.get("description"):
            parts.append(f"About: {lead['description']}")

        if lead.get("technologies"):
            parts.append(f"Technologies: {', '.join(lead['technologies'][:5])}")

        if lead.get("keywords"):
            parts.append(f"Keywords: {', '.join(lead['keywords'][:5])}")

        if lead.get("signal_context"):
            parts.append(f"Signal: {lead['signal_context']}")

        return "\n".join(parts) if parts else "Limited information available about this company."


def calculate_lead_score(lead: dict) -> int:
    """
    Calculate a lead quality score (1-10).

    Args:
        lead: Lead data dictionary

    Returns:
        Score from 1-10
    """
    score = 0

    # Has verified email (+2)
    if lead.get("email"):
        score += 2

    # Has website (+1)
    if lead.get("website"):
        score += 1

    # Has phone (+1)
    if lead.get("phone"):
        score += 1

    # Employee count in sweet spot (10-100) (+2)
    emp_count = lead.get("employee_count", 0)
    if isinstance(emp_count, str):
        try:
            emp_count = int(emp_count.replace(",", "").split("-")[0])
        except:
            emp_count = 0

    if 10 <= emp_count <= 100:
        score += 2
    elif 5 <= emp_count <= 200:
        score += 1

    # Industry match (+2)
    good_industries = ["engineering", "architecture", "environmental", "consulting", "construction", "geotechnical", "civil"]
    industry = (lead.get("industry") or "").lower()
    if any(ind in industry for ind in good_industries):
        score += 2

    # Has LinkedIn (+1)
    if lead.get("linkedin"):
        score += 1

    return min(score, 10)


def calculate_startup_lead_score(lead: dict) -> int:
    """
    Calculate a lead quality score for B2B startup leads (1-10).

    Scoring:
    - Multi-signal (2+ sources): +3
    - Signal type: apollo_hiring/hiring_signal +2, apollo_has_sdrs +1
    - Employee count 5-50 (sweet spot): +2
    - Has verified email: +1
    - Has website: +1
    - B2B SaaS keywords in company data: +1
    - Max score: 10
    """
    score = 0

    # Multi-signal — strongest ICP indicator
    if lead.get("multi_signal"):
        score += 3

    # Signal type (intent indicator)
    signal = lead.get("signal_type", "")
    if signal in ("apollo_hiring", "hiring_signal"):
        score += 2
    elif signal == "apollo_has_sdrs":
        score += 1

    # Employee count in sweet spot (5-50)
    emp_count = lead.get("employee_count", 0)
    if isinstance(emp_count, str):
        try:
            emp_count = int(str(emp_count).replace(",", "").split("-")[0])
        except (ValueError, TypeError):
            emp_count = 0

    if 5 <= emp_count <= 50:
        score += 2

    # Has verified email (+1)
    if lead.get("email"):
        score += 1

    # Has website (+1)
    if lead.get("website"):
        score += 1

    # B2B SaaS keywords in company data (+1)
    b2b_keywords = {"saas", "b2b", "software", "platform", "api", "cloud", "enterprise", "automation"}
    company_text = " ".join(str(k).lower() for k in (lead.get("keywords") or [])) + " " + (lead.get("description") or "").lower()
    if any(kw in company_text for kw in b2b_keywords):
        score += 1

    return min(score, 10)
