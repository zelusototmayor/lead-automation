"""Metrics calculations for the dashboard."""

from collections import Counter
from datetime import datetime, date
from typing import Any


def calculate_metrics(rows: list[list[str]]) -> dict[str, Any]:
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
        "last_updated": datetime.now().strftime("%H:%M, %d %b %Y"),
    }


def _row_to_dict(row: list) -> dict:
    """Convert a row to a dictionary using ACTUAL sheet column order.

    Real columns (verified against live sheet 2026-02-20):
    0  ID
    1  Company
    2  Contact Name
    3  Email
    4  Phone
    5  Contacted       ← extra col that shifted everything
    6  Website
    7  Industry
    8  Employee Count
    9  City
    10 Country
    11 Lead Score
    12 Status
    13 Date Added
    14 Last Contact
    15 Email 1 Sent
    16 Email 2 Sent
    17 Email 3 Sent
    18 Email 4 Sent
    19 Opens          ← new col
    20 Clicks         ← new col
    21 Response
    22 Notes
    23 Source
    24 LinkedIn
    25 Title
    """
    while len(row) < 26:
        row.append("")

    return {
        "id":            row[0],
        "company":       row[1],
        "contact_name":  row[2],
        "email":         row[3],
        "phone":         row[4],
        "contacted_flag":row[5],
        "website":       row[6],
        "industry":      row[7],
        "employee_count":row[8],
        "city":          row[9],
        "country":       row[10],
        "lead_score":    row[11],
        "status":        row[12],
        "date_added":    row[13],
        "last_contact":  row[14],
        "email_1_sent":  row[15],
        "email_2_sent":  row[16],
        "email_3_sent":  row[17],
        "email_4_sent":  row[18],
        "opens":         row[19],
        "clicks":        row[20],
        "response":      row[21],
        "notes":         row[22],
        "source":        row[23],
        "linkedin":      row[24],
        "title":         row[25],
    }


def _calculate_summary(leads: list[dict], today: date) -> dict:
    total = len(leads)

    # Emails sent today (last_contact date = today)
    sent_today = 0
    for lead in leads:
        if lead["last_contact"]:
            try:
                lc_date = datetime.strptime(lead["last_contact"].split()[0], "%Y-%m-%d").date()
                if lc_date == today:
                    sent_today += 1
            except (ValueError, IndexError):
                pass

    # Contacts with at least email 1 sent
    contacted = sum(1 for lead in leads if lead["email_1_sent"] == "TRUE")

    # Real replies: response field has actual text (not empty, not TRUE/FALSE)
    def is_real_reply(r: str) -> bool:
        if not r:
            return False
        r_clean = r.strip().upper()
        return r_clean not in ("", "TRUE", "FALSE", "N/A", "-", "NONE")

    responses = sum(1 for lead in leads if is_real_reply(lead["response"]))

    # Open rate
    opens = sum(1 for lead in leads if lead.get("opens", "0") not in ("", "0"))

    # Response rate against contacted
    response_rate = round((responses / contacted * 100), 1) if contacted > 0 else 0.0
    open_rate = round((opens / contacted * 100), 1) if contacted > 0 else 0.0

    return {
        "leads_today":    sent_today,
        "total_leads":    total,
        "total_responses":responses,
        "response_rate":  response_rate,
        "contacted":      contacted,
        "opens":          opens,
        "open_rate":      open_rate,
    }


def _calculate_pipeline(leads: list[dict]) -> dict:
    status_counts = Counter(
        lead["status"].strip().lower()
        for lead in leads
        if lead["status"]
    )

    queued = sum(
        1 for lead in leads
        if lead["status"].strip().lower() == "new"
        and lead["email"]
        and lead["email_1_sent"] != "TRUE"
    )

    return {
        "new":       status_counts.get("new", 0),
        "queued":    queued,
        "contacted": status_counts.get("contacted", 0),
        "replied":   status_counts.get("replied", 0),
        "won":       status_counts.get("won", 0),
        "lost":      status_counts.get("lost", 0),
    }


def _calculate_email_sequence(leads: list[dict]) -> list[dict]:
    sequence = []

    for step in range(1, 5):
        sent_key = f"email_{step}_sent"
        sent = sum(1 for lead in leads if lead.get(sent_key) == "TRUE")

        responses = 0
        for lead in leads:
            if lead["response"] and lead["response"].strip().upper() not in ("TRUE", "FALSE", "") and lead.get(sent_key) == "TRUE":
                next_key = f"email_{step + 1}_sent" if step < 4 else None
                if next_key is None or lead.get(next_key) != "TRUE":
                    responses += 1

        response_rate = round((responses / sent * 100), 1) if sent > 0 else 0.0

        sequence.append({
            "step": step,
            "name": "Initial" if step == 1 else f"Follow-up {step - 1}",
            "sent": sent,
            "responses": responses,
            "response_rate": response_rate,
        })

    return sequence


def _calculate_score_distribution(leads: list[dict]) -> dict:
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
        "data":   [scores.get(i, 0) for i in range(1, 11)],
    }


def _calculate_industry_breakdown(leads: list[dict]) -> dict:
    industries = Counter(
        lead["industry"].strip()
        for lead in leads
        if lead["industry"] and lead["industry"].strip()
    )
    top_10 = industries.most_common(10)
    return {
        "labels": [item[0] for item in top_10],
        "data":   [item[1] for item in top_10],
    }


def _calculate_company_size(leads: list[dict]) -> dict:
    buckets = {"1–10": 0, "11–50": 0, "51–200": 0, "201–500": 0, "500+": 0}
    for lead in leads:
        if lead["employee_count"]:
            try:
                n = int(lead["employee_count"].replace(",", "").replace("+", ""))
                if n <= 10:      buckets["1–10"] += 1
                elif n <= 50:    buckets["11–50"] += 1
                elif n <= 200:   buckets["51–200"] += 1
                elif n <= 500:   buckets["201–500"] += 1
                else:            buckets["500+"] += 1
            except ValueError:
                pass

    return {
        "labels": list(buckets.keys()),
        "data":   list(buckets.values()),
    }


def _calculate_geography(leads: list[dict]) -> dict:
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
        "cities":    cities.most_common(10),
    }
