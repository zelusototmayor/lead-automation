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

        prompt = f"""You are helping write a personalized cold email for a boutique automation agency.

SENDER INFORMATION:
{sender_info.get('bio', 'An automation specialist helping agencies eliminate manual work.')}

VALUE PROPOSITION:
{sender_info.get('value_proposition', 'Helping agencies automate repetitive processes.')}

LEAD INFORMATION:
{lead_context}

YOUR TASK:
Generate personalized email components for this lead. Be specific, reference real details about their company, and identify relevant pain points.

Return a JSON object with these fields:
1. "personalized_opener": 1-2 sentences referencing something specific about their company or work. Don't be generic.
2. "specific_pain_point": 1-2 sentences about a likely automation opportunity based on what they do. Be concrete.
3. "industry_specific_insight": A valuable observation about automation trends in their industry.
4. "suggested_subject": A compelling, non-spammy subject line.

Guidelines:
- Be conversational, not salesy
- Reference specific details (their services, size, industry)
- The opener should show you did research
- Pain points should be realistic for agencies of their size/type
- Keep each component concise (1-3 sentences max)
- Avoid clichés like "I hope this finds you well" or "I noticed your company"

Return ONLY valid JSON, no other text."""

        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=500,
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
            # Return fallback content
            return {
                "personalized_opener": f"I came across {lead.get('company', 'your agency')} and was impressed by your work in {lead.get('industry', 'the industry')}.",
                "specific_pain_point": "Many agencies spend hours each week on manual data entry and reporting that could be fully automated.",
                "industry_specific_insight": "Agencies that automate their lead management typically see 40% more time for client work.",
                "suggested_subject": f"Quick question for {lead.get('company', 'your team')}"
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
    good_industries = ["marketing", "advertising", "media", "communications", "pr", "creative", "digital"]
    industry = (lead.get("industry") or "").lower()
    if any(ind in industry for ind in good_industries):
        score += 2

    # Has LinkedIn (+1)
    if lead.get("linkedin"):
        score += 1

    return min(score, 10)
