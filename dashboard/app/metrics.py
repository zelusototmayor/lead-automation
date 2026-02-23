"""Metrics calculations for the dashboard."""

from collections import Counter
from datetime import date, datetime
from typing import Any


def calculate_metrics(rows: list[list[str]], header: list[str] | None = None) -> dict[str, Any]:
    leads = [_row_to_dict(row, header) for row in rows]
    today = date.today()

    return {
        "summary": _calculate_summary(leads, today),
        "pipeline": _calculate_pipeline(leads),
        "email_sequence": _calculate_email_sequence(leads),
        "score_distribution": _calculate_score_distribution(leads),
        "industry_breakdown": _calculate_industry_breakdown(leads),
        "company_size": _calculate_company_size(leads),
        "geography": _calculate_geography(leads),
        "trend": _calculate_trend(leads, today),
        "last_updated": datetime.now().strftime("%H:%M, %d %b %Y"),
    }


def normalize_rows(rows: list[list[str]], header: list[str] | None = None) -> list[dict]:
    return [_row_to_dict(row, header) for row in rows]


def _row_to_dict(row: list, header: list[str] | None = None) -> dict:
    """Drift-tolerant row parser for live sheet quirks."""

    def txt(i: int) -> str:
        return row[i].strip() if i < len(row) else ""

    def is_dt(v: str) -> bool:
        if not v:
            return False
        try:
            datetime.strptime(v.split()[0], "%Y-%m-%d")
            return True
        except Exception:
            return False

    def is_url(v: str) -> bool:
        v = (v or "").lower()
        return v.startswith("http://") or v.startswith("https://")

    def is_email(v: str) -> bool:
        return "@" in (v or "") and "." in (v or "")

    def is_bool(v: str) -> bool:
        return (v or "").strip().upper() in ("TRUE", "FALSE")

    out = {
        "id": txt(0),
        "company": txt(1),
        "contact_name": txt(2),
        "email": txt(3),
        "phone": txt(4),
        "status": txt(5),
        "notes": txt(6),
        "website": txt(7) if is_url(txt(7)) else "",
        "industry": txt(8),
        "employee_count": txt(9),
        "city": txt(10),
        "country": txt(11),
        "lead_score": "",
        "date_added": "",
        "last_contact": "",
        "email_1_sent": "",
        "email_2_sent": "",
        "email_3_sent": "",
        "email_4_sent": "",
        "opens": "",
        "clicks": "",
        "response": "",
        "source": "",
        "linkedin": "",
        "title": "",
    }

    for i in range(12, min(len(row), 23)):
        v = txt(i)
        if not out["date_added"] and is_dt(v):
            out["date_added"] = v
        elif is_bool(v):
            if not out["email_1_sent"]:
                out["email_1_sent"] = v
            elif not out["email_2_sent"]:
                out["email_2_sent"] = v
            elif not out["email_3_sent"]:
                out["email_3_sent"] = v
            elif not out["email_4_sent"]:
                out["email_4_sent"] = v
        elif v.isdigit() and out["opens"] == "":
            out["opens"] = v
        elif v.isdigit() and out["clicks"] == "":
            out["clicks"] = v
        elif "repl" in v.lower() and not out["response"]:
            out["response"] = v

    for i in range(22, len(row)):
        v = txt(i)
        if not v:
            continue
        if "linkedin.com" in v.lower() and not out["linkedin"]:
            out["linkedin"] = v
        elif is_email(v) and not out["email"]:
            out["email"] = v
        elif any(k in v.lower() for k in ["google_maps", "apollo", "manually", "import"]) and not out["source"]:
            out["source"] = v
        elif any(k in v.lower() for k in ["ceo", "founder", "director", "manager", "owner", "president"]) and not out["title"]:
            out["title"] = v
        elif v.lower() in ("active", "completed", "unknown (-1)"):
            pass
        elif "repl" in v.lower() and not out["response"]:
            out["response"] = v

    if not out["last_contact"]:
        out["last_contact"] = out["date_added"]

    return out


def _calculate_summary(leads: list[dict], today: date) -> dict:
    total = len(leads)

    leads_today = 0
    for lead in leads:
        if lead["date_added"]:
            try:
                lead_date = datetime.strptime(lead["date_added"].split()[0], "%Y-%m-%d").date()
                if lead_date == today:
                    leads_today += 1
            except (ValueError, IndexError):
                pass

    def is_real_reply(v: str) -> bool:
        if not v:
            return False
        t = str(v).strip().upper()
        return t not in ("", "0", "FALSE", "NO", "N/A", "NONE", "-")

    responses = sum(1 for lead in leads if is_real_reply(lead.get("response", "")))
    contacted = sum(1 for lead in leads if str(lead.get("email_1_sent", "")).upper() == "TRUE")
    opens = sum(1 for lead in leads if str(lead.get("opens", "")).strip() not in ("", "0"))

    response_rate = (responses / contacted * 100) if contacted > 0 else 0
    open_rate = (opens / contacted * 100) if contacted > 0 else 0

    return {
        "leads_today": leads_today,
        "total_leads": total,
        "total_responses": responses,
        "response_rate": round(response_rate, 1),
        "contacted": contacted,
        "opens": opens,
        "open_rate": round(open_rate, 1),
    }


def _calculate_pipeline(leads: list[dict]) -> dict:
    status_counts = Counter((lead.get("status", "") or "").strip().lower() for lead in leads if lead.get("status"))

    def has_real_reply(lead: dict) -> bool:
        v = str(lead.get("response", "")).strip().lower()
        return bool(v) and v not in ("0", "false", "none", "n/a", "-")

    contacted = sum(1 for lead in leads if str(lead.get("email_1_sent", "")).upper() == "TRUE")
    replied = sum(1 for lead in leads if has_real_reply(lead))
    won = status_counts.get("won", 0)
    lost = status_counts.get("lost", 0)
    queued = sum(1 for lead in leads if lead.get("email") and str(lead.get("email_1_sent", "")).upper() != "TRUE")
    new = max(0, len(leads) - contacted)

    return {
        "new": new,
        "queued": queued,
        "contacted": contacted,
        "replied": replied,
        "won": won,
        "lost": lost,
    }


def _calculate_email_sequence(leads: list[dict]) -> list[dict]:
    sequence = []

    for step in range(1, 5):
        sent_key = f"email_{step}_sent"
        sent = sum(1 for lead in leads if lead.get(sent_key) == "TRUE")

        responses = 0
        for lead in leads:
            if lead["response"] and lead.get(sent_key) == "TRUE":
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
    industries = Counter(lead["industry"].strip() for lead in leads if lead["industry"] and lead["industry"].strip())
    top_10 = industries.most_common(10)
    return {
        "labels": [item[0] for item in top_10],
        "data": [item[1] for item in top_10],
    }


def _calculate_company_size(leads: list[dict]) -> dict:
    buckets = {"1–10": 0, "11–50": 0, "51–200": 0, "201–500": 0, "500+": 0}
    for lead in leads:
        if lead["employee_count"]:
            try:
                n = int(lead["employee_count"].replace(",", "").replace("+", ""))
                if n <= 10:
                    buckets["1–10"] += 1
                elif n <= 50:
                    buckets["11–50"] += 1
                elif n <= 200:
                    buckets["51–200"] += 1
                elif n <= 500:
                    buckets["201–500"] += 1
                else:
                    buckets["500+"] += 1
            except ValueError:
                pass

    return {
        "labels": list(buckets.keys()),
        "data": list(buckets.values()),
    }


def _calculate_geography(leads: list[dict]) -> dict:
    countries = Counter(lead["country"].strip() for lead in leads if lead["country"] and lead["country"].strip())
    cities = Counter(lead["city"].strip() for lead in leads if lead["city"] and lead["city"].strip())
    return {
        "countries": countries.most_common(10),
        "cities": cities.most_common(10),
    }


def _calculate_trend(leads: list[dict], today: date) -> dict:
    days = [today.fromordinal(today.toordinal() - i) for i in range(13, -1, -1)]
    sent_map = {d: 0 for d in days}
    reply_map = {d: 0 for d in days}

    def is_real_reply(v: str) -> bool:
        if not v:
            return False
        t = str(v).strip().upper()
        return t not in ("", "0", "FALSE", "NO", "N/A", "NONE", "-")

    for lead in leads:
        raw = (lead.get("last_contact") or lead.get("date_added") or "").strip()
        if not raw:
            continue
        try:
            d = datetime.strptime(raw.split()[0], "%Y-%m-%d").date()
        except (ValueError, IndexError):
            continue
        if d in sent_map:
            sent_map[d] += 1
            if is_real_reply(lead.get("response", "")):
                reply_map[d] += 1

    return {
        "labels": [d.strftime("%d %b") for d in days],
        "sent": [sent_map[d] for d in days],
        "replies": [reply_map[d] for d in days],
    }
