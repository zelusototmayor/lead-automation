"""Real read-only Google transports. No send method and no implicit account choice.

Run Gmail transport on the Mac mini when broad mailbox credentials must remain
there; submit its bounded observations to the scoped CRM provider endpoint.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from email.utils import getaddresses
import hashlib
import io
import re
from urllib.parse import quote

from google.auth.transport.requests import AuthorizedSession
from src.crm.google_credentials import load_google_credentials

GMAIL_READ_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
CALENDAR_READ_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
PROPOSAL_NAME = re.compile(
    r"\b(propost[ao]|proposal|orçamento|orcamento|quotation|quote)\b", re.I
)


class GoogleTransportError(RuntimeError):
    pass


def _decode(value):
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _parts(part):
    yield part
    for child in part.get("parts", []):
        yield from _parts(child)


def extract_amounts(text):
    """Only explicit labelled values become candidates; ambiguity stays unknown."""
    currency = "EUR" if re.search(r"\bEUR\b|€", text, re.I) else None
    if re.search(r"\bUSD\b|\$", text, re.I):
        currency = "USD" if currency is None else None
    result = {
        "currency": currency,
        "one_off_amount": None,
        "mrr_amount": None,
        "arr_amount": None,
        "value_ambiguous": True,
    }
    if currency is None:
        return result
    for target, label in (
        ("one_off_amount", r"(?:total|implementa[çc][ãa]o|setup|initial|inicial)"),
        ("mrr_amount", r"(?:mensal|monthly|por m[eê]s|MRR)"),
        ("arr_amount", r"(?:anual|annual|ARR)"),
    ):
        values = re.findall(
            label
            + r"\s*[:\-]?\s*(?:EUR|€|USD|\$)?\s*([0-9][0-9 .,]*[0-9]|[0-9])\s*(?:EUR|€|USD|\$)",
            text,
            re.I,
        )
        normalized = set()
        for value in values:
            value = value.replace(" ", "")
            if "," in value and "." in value:
                if value.rfind(",") > value.rfind("."):
                    value = value.replace(".", "").replace(",", ".")
                else:
                    value = value.replace(",", "")
            elif "," in value:
                value = (
                    value.replace(",", ".")
                    if len(value.rsplit(",", 1)[1]) <= 2
                    else value.replace(",", "")
                )
            if re.fullmatch(r"\d+(?:\.\d{1,2})?", value):
                normalized.add(value)
        if len(normalized) == 1:
            result[target] = normalized.pop()
        elif len(normalized) > 1:
            return result | {
                "one_off_amount": None,
                "mrr_amount": None,
                "arr_amount": None,
            }
    result["value_ambiguous"] = not any(
        result[name] is not None
        for name in ("one_off_amount", "mrr_amount", "arr_amount")
    )
    return result


class _GoogleReadTransport:
    def __init__(self, *, credentials_file, scopes, session=None):
        self._credentials_file, self._scopes = credentials_file, scopes
        self._session = session

    def _get(self, url, **params):
        if self._session is None:
            self._session = AuthorizedSession(
                load_google_credentials(self._credentials_file, self._scopes)
            )
        response = self._session.get(url, params=params or None, timeout=20)
        response.raise_for_status()
        return response.json()


class GmailTransport(_GoogleReadTransport):
    """Bounded scan includes Sent and archived mail; never depends on unread state."""

    def __init__(
        self, *, credentials_file, mailbox_email, since=None, limit=20, session=None
    ):
        super().__init__(
            credentials_file=credentials_file,
            scopes=[GMAIL_READ_SCOPE],
            session=session,
        )
        if not mailbox_email or "@" not in mailbox_email or not 1 <= limit <= 20:
            raise GoogleTransportError("Explicit mailbox and limit are required")
        self.mailbox_email = mailbox_email.lower()
        self.since = since or datetime.now(UTC) - timedelta(days=7)
        if self.since.tzinfo is None:
            raise GoogleTransportError("Sync start requires timezone")
        self.limit = limit

    def fetch(self, scope, cursor=None):
        if scope not in {self.mailbox_email, "mailbox:" + self.mailbox_email}:
            raise GoogleTransportError("Mailbox scope mismatch")
        base = "https://gmail.googleapis.com/gmail/v1/users/me"
        profile = self._get(base + "/profile")
        if profile.get("emailAddress", "").lower() != self.mailbox_email:
            raise GoogleTransportError("Authenticated mailbox mismatch")
        params = {
            "q": f"after:{int(self.since.timestamp())}",
            "maxResults": self.limit,
            "includeSpamTrash": "false",
        }
        if cursor:
            params["pageToken"] = cursor
        page = self._get(base + "/messages", **params)
        observations = []
        for ref in page.get("messages", []):
            message = self._get(
                base + "/messages/" + quote(ref["id"], safe=""), format="full"
            )
            payload = message.get("payload", {})
            headers = {
                h["name"].lower(): h["value"] for h in payload.get("headers", [])
            }
            direction = (
                "outbound" if "SENT" in message.get("labelIds", []) else "inbound"
            )
            addresses = getaddresses(
                [headers.get("to" if direction == "outbound" else "from", "")]
            )
            counterparts = sorted(
                {
                    address.lower()
                    for _, address in addresses
                    if address and address.lower() != self.mailbox_email
                }
            )
            subject = (
                headers.get("subject", "(sem assunto)")
                .encode("utf-8")[:512]
                .decode("utf-8", "ignore")
                or "(sem assunto)"
            )
            pieces = list(_parts(payload))
            body = "\n".join(
                _decode(p.get("body", {}).get("data", "")).decode("utf-8", "replace")
                for p in pieces
                if p.get("mimeType") == "text/plain"
            )
            attachments = []
            for part in pieces:
                filename = part.get("filename", "")
                if (
                    not filename
                    or not PROPOSAL_NAME.search(filename + " " + subject)
                    or int(part.get("body", {}).get("size", 0)) > 10_000_000
                ):
                    continue
                raw = part.get("body", {}).get("data")
                if not raw and part.get("body", {}).get("attachmentId"):
                    raw = self._get(
                        base
                        + f"/messages/{quote(message['id'], safe='')}/attachments/{quote(part['body']['attachmentId'], safe='')}"
                    ).get("data", "")
                if not raw:
                    continue
                data = _decode(raw)
                if len(data) > 10_000_000:
                    continue
                document_text = ""
                if filename.lower().endswith(".pdf"):
                    try:
                        from pypdf import PdfReader

                        document_text = "\n".join(
                            (page.extract_text() or "")
                            for page in PdfReader(io.BytesIO(data)).pages[:20]
                        )[:100000]
                    except Exception:
                        document_text = ""
                attachments.append(
                    {
                        "name": filename[:512],
                        "content_hash": hashlib.sha256(data).hexdigest(),
                        **extract_amounts(document_text or body),
                    }
                )
            observations.append(
                {
                    "provider": "gmail",
                    "source_scope": scope,
                    "id": message["id"],
                    "thread_id": message["threadId"],
                    "occurred_at": datetime.fromtimestamp(
                        int(message["internalDate"]) / 1000, UTC
                    ).isoformat(),
                    "direction": direction,
                    "contact_emails": counterparts[:10],
                    "subject": subject,
                    "excerpt": body[:2000] or message.get("snippet", "")[:2000],
                    "attachments": attachments[:5],
                    "bulk": bool(
                        headers.get("list-id")
                        or headers.get("list-unsubscribe")
                        or headers.get("precedence", "").lower()
                        in {"bulk", "list", "junk"}
                        or headers.get("auto-submitted", "no").lower() != "no"
                    ),
                    "evidence": {
                        "provider": "gmail",
                        "message_id": message["id"],
                        "thread_id": message["threadId"],
                        "mailbox": self.mailbox_email,
                    },
                }
            )
        return {
            "observations": observations,
            "next_cursor": page.get("nextPageToken"),
            "scanned_since": self.since.isoformat(),
            "mailbox": self.mailbox_email,
        }


class CalendarTransport(_GoogleReadTransport):
    def __init__(
        self, *, credentials_file, calendar_id, since=None, limit=20, session=None
    ):
        super().__init__(
            credentials_file=credentials_file,
            scopes=[CALENDAR_READ_SCOPE],
            session=session,
        )
        self.calendar_id, self.since, self.limit = (
            calendar_id,
            since or datetime.now(UTC) - timedelta(days=7),
            limit,
        )

    def fetch(self, scope, cursor=None):
        if scope != self.calendar_id or not 1 <= self.limit <= 20:
            raise GoogleTransportError("Calendar scope mismatch")
        params = {
            "timeMin": self.since.isoformat(),
            "maxResults": self.limit,
            "singleEvents": "true",
            "showDeleted": "true",
        }
        if cursor:
            params["pageToken"] = cursor
        page = self._get(
            f"https://www.googleapis.com/calendar/v3/calendars/{quote(self.calendar_id, safe='')}/events",
            **params,
        )
        return {
            "observations": [
                {
                    "provider": "google_calendar",
                    "source_scope": scope,
                    "id": event["id"],
                    "occurred_at": event.get("updated"),
                    "status": event.get("status"),
                    "title": event.get("summary", "")[:512],
                    "start": event.get("start"),
                    "end": event.get("end"),
                    "attendees": [
                        item.get("email") for item in event.get("attendees", [])
                    ][:20],
                }
                for event in page.get("items", [])
            ],
            "next_cursor": page.get("nextPageToken"),
            "scanned_since": self.since.isoformat(),
        }
