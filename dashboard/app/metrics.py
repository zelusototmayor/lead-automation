"""Metrics calculations for the dashboard."""

from collections import Counter
from datetime import datetime, date
from typing import Any


def calculate_metrics(rows: list[list[str]]) -> dict[str, Any]:
    """
    Calculate all dashboard metrics from raw CRM data.

    Args:
        rows: List of rows from Google Sheets (excluding header)

    Returns:
        Dictionary containing all calculated metrics
    """
    leads = [_row_to_dict(row) for row in rows]
    today = date.today()

    return {
        "summary": _calculate_summary(leads, today),
        "pipeline": _calculate_pipeline(leads),
        "email_sequence": _calculate_email_sequence(leads),
        "score_distribution": _calculate_score_distribution(leads),
        "industry_breakdown": _calculate_industry_breakdown(leads),
        "company_size": _calculate_company_size(leads),
        "geography": _calculate_geography(leads),
        "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def _row_to_dict(row: list) -> dict:
    """Convert a row to a dictionary."""
    # Pad row if needed
    while len(row) < 23:
        row.append("")

    return {
        "id": row[0],
        "company": row[1],
        "contact_name": row[2],
        "email": row[3],
        "phone": row[4],
        "website": row[5],
        "industry": row[6],
        "employee_count": row[7],
        "city": row[8],
        "country": row[9],
        "lead_score": row[10],
        "status": row[11],
        "date_added": row[12],
        "last_contact": row[13],
        "email_1_sent": row[14],
        "email_2_sent": row[15],
        "email_3_sent": row[16],
        "email_4_sent": row[17],
        "response": row[18],
        "notes": row[19],
        "source": row[20],
        "linkedin": row[21] if len(row) > 21 else "",
        "title": row[22] if len(row) > 22 else "",
    }


def _calculate_summary(leads: list[dict], today: date) -> dict:
    """Calculate executive summary metrics."""
    total = len(leads)

    # Count leads added today
    leads_today = 0
    for lead in leads:
        if lead["date_added"]:
            try:
                lead_date = datetime.strptime(lead["date_added"].split()[0], "%Y-%m-%d").date()
                if lead_date == today:
                    leads_today += 1
            except (ValueError, IndexError):
                pass

    # Count responses
    responses = sum(1 for lead in leads if lead["response"])

    # Response rate (only for contacted leads)
    contacted = sum(1 for lead in leads if lead["email_1_sent"] == "TRUE")
    response_rate = (responses / contacted * 100) if contacted > 0 else 0

    return {
        "leads_today": leads_today,
        "total_leads": total,
        "total_responses": responses,
        "response_rate": round(response_rate, 1),
        "contacted": contacted,
    }


def _calculate_pipeline(leads: list[dict]) -> dict:
    """Calculate pipeline status counts."""
    status_counts = Counter(lead["status"].lower() for lead in leads if lead["status"])

    # Also count "Queued" as leads with email but not yet contacted
    queued = sum(
        1 for lead in leads
        if lead["status"].lower() == "new"
        and lead["email"]
        and lead["email_1_sent"] != "TRUE"
    )

    return {
        "new": status_counts.get("new", 0),
        "queued": queued,
        "contacted": status_counts.get("contacted", 0),
        "replied": status_counts.get("replied", 0),
        "won": status_counts.get("won", 0),
        "lost": status_counts.get("lost", 0),
    }


def _calculate_email_sequence(leads: list[dict]) -> list[dict]:
    """Calculate email sequence performance."""
    sequence = []

    for step in range(1, 5):
        sent_key = f"email_{step}_sent"
        sent = sum(1 for lead in leads if lead.get(sent_key) == "TRUE")

        # Responses after this email (approximate - assumes response came after last email sent)
        responses = 0
        for lead in leads:
            if lead["response"] and lead.get(sent_key) == "TRUE":
                # Check if this was the last email sent
                next_key = f"email_{step + 1}_sent" if step < 4 else None
                if next_key is None or lead.get(next_key) != "TRUE":
                    responses += 1

        response_rate = (responses / sent * 100) if sent > 0 else 0

        sequence.append({
            "step": step,
            "name": f"Email {step}" if step == 1 else f"Follow-up {step - 1}",
            "sent": sent,
            "responses": responses,
            "response_rate": round(response_rate, 1),
        })

    return sequence


def _calculate_score_distribution(leads: list[dict]) -> dict:
    """Calculate lead score distribution."""
    scores = Counter()

    for lead in leads:
        if lead["lead_score"]:
            try:
                score = int(float(lead["lead_score"]))
                if 1 <= score <= 10:
                    scores[score] += 1
            except ValueError:
                pass

    return {
        "labels": list(range(1, 11)),
        "data": [scores.get(i, 0) for i in range(1, 11)],
    }


def _calculate_industry_breakdown(leads: list[dict]) -> dict:
    """Calculate industry breakdown (top 10)."""
    industries = Counter(
        lead["industry"].strip()
        for lead in leads
        if lead["industry"] and lead["industry"].strip()
    )

    top_10 = industries.most_common(10)

    return {
        "labels": [item[0] for item in top_10],
        "data": [item[1] for item in top_10],
    }


def _calculate_company_size(leads: list[dict]) -> dict:
    """Calculate company size distribution."""
    size_buckets = {
        "1-10": 0,
        "11-50": 0,
        "51-200": 0,
        "201-500": 0,
        "501-1000": 0,
        "1000+": 0,
    }

    for lead in leads:
        if lead["employee_count"]:
            try:
                count = int(lead["employee_count"].replace(",", "").replace("+", ""))
                if count <= 10:
                    size_buckets["1-10"] += 1
                elif count <= 50:
                    size_buckets["11-50"] += 1
                elif count <= 200:
                    size_buckets["51-200"] += 1
                elif count <= 500:
                    size_buckets["201-500"] += 1
                elif count <= 1000:
                    size_buckets["501-1000"] += 1
                else:
                    size_buckets["1000+"] += 1
            except ValueError:
                pass

    return {
        "labels": list(size_buckets.keys()),
        "data": list(size_buckets.values()),
    }


def _calculate_geography(leads: list[dict]) -> dict:
    """Calculate geographic breakdown."""
    countries = Counter(
        lead["country"].strip()
        for lead in leads
        if lead["country"] and lead["country"].strip()
    )

    cities = Counter(
        lead["city"].strip()
        for lead in leads
        if lead["city"] and lead["city"].strip()
    )

    return {
        "countries": countries.most_common(10),
        "cities": cities.most_common(10),
    }
