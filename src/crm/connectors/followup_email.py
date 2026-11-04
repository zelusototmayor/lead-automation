"""Fresh complete Gmail evidence. Search never substitutes for direct drafts.

GET traversal is bounded by a deadline, not a silently truncated row count.
Caller must refuse incomplete evidence. This module does not use legacy sender
transports, whose POST retries have different semantics.
"""

import base64
from collections import OrderedDict
import email.policy
import json
import logging
import os
import uuid
from threading import Lock
import time
from datetime import UTC, datetime
from email.parser import BytesParser
from email.utils import getaddresses
from urllib.parse import quote

from src.crm.connectors.note_drafts import valid_address
from src.crm.connectors.gmail_evidence import (
    ImmutableEvidenceCache, cache_scope, credential_generation, evidence_key,
)
from src.crm.connectors.gmail_cooldown import GmailRateLimited, check as check_cooldown, observe as observe_quota


# Process-local, shared across the request-scoped GmailProvider instances.
# No labels, membership, body, signature, recipient roles or review is cached.
_HEADER_TTL = 3600
_HEADER_LIMIT = 2048
_HEADER_CACHE = OrderedDict()
_HEADER_LOCK = Lock()
_CONTENT_CACHE = ImmutableEvidenceCache()
# Private, fixed-schema operational evidence. No source IDs, addresses, paths,
# arbitrary provider messages or credentials enter this record.
_DIAG_BOOT = uuid.uuid4().hex
_DIAG_START = time.monotonic()
_DIAG_COUNTS = {"get_attempts": 0, "cache_hits": 0, "cache_misses": 0,
                "cache_unusable": 0, "cache_expired": 0, "cache_evictions": 0}


def _family(path, params=None):
    if path == "/profile": return "profile"
    if path == "/settings/sendAs": return "settings_sendAs"
    if path == "/messages": return "messages_list"
    if path == "/drafts": return "drafts_list"
    if path.startswith("/messages/"):
        return {"raw": "message_raw", "metadata": "message_metadata"}.get(
            (params or {}).get("format"), "message_other")
    if path.startswith("/threads/"): return "thread_read"
    return "other"


def _private_event(provider, outcome, *, family=None, receipt=None):
    try:
        with _HEADER_LOCK:
            counters = dict(_DIAG_COUNTS)
            counters["cache_entries"] = len(_HEADER_CACHE)
        stats = dict(getattr(provider, "_private_read_stats", {}))
        event = {"event": "gmail_read_diagnostic_v1", "boot": _DIAG_BOOT,
                 "pid": os.getpid(), "actor": "followup_context",
                 "outcome": outcome, "endpoint_family": family,
                 "process_age_seconds": round(time.monotonic() - _DIAG_START, 3),
                 "process_counters": counters, "read_counters": stats}
        if receipt is not None:
            # Re-sanitize even though GmailRateLimited already applies this gate.
            from src.crm.connectors.gmail_cooldown import _safe_receipt
            clean = _safe_receipt(receipt)
            event["google"] = {k: clean[k] for k in ("http_status", "error_code",
                "error_reasons", "error_status", "quota_details", "google_details_present",
                "google_quota_message_present") if k in clean}
        logging.getLogger("crm.gmail.private").warning(json.dumps(event, separators=(",", ":")))
    except Exception:
        # Observability cannot convert a successful read or hide quota refusal.
        pass


def _count_get(provider, family):
    with _HEADER_LOCK:
        _DIAG_COUNTS["get_attempts"] += 1
    stats = getattr(provider, "_private_read_stats", None)
    if stats is not None:
        stats["get_attempts"] += 1
        stats["endpoint_counts"][family] = stats["endpoint_counts"].get(family, 0) + 1



def _header_key(provider, mailbox, message_id):
    # Same loaded grant/session generation as content reuse; profile verifies
    # mailbox before either cache is consulted. Unknown identity means miss.
    return evidence_key(cache_scope(provider, mailbox), message_id)


def _cached_addresses(key):
    if key is None:
        with _HEADER_LOCK:
            _DIAG_COUNTS["cache_unusable"] += 1
        return None
    with _HEADER_LOCK:
        entry = _HEADER_CACHE.get(key)
        if entry is None:
            _DIAG_COUNTS["cache_misses"] += 1
            return None
        expires, addresses = entry
        if expires <= time.monotonic():
            del _HEADER_CACHE[key]
            _DIAG_COUNTS["cache_expired"] += 1
            return None
        _HEADER_CACHE.move_to_end(key)
        _DIAG_COUNTS["cache_hits"] += 1
        return addresses


def _remember_addresses(entries):
    now = time.monotonic()
    with _HEADER_LOCK:
        for key, addresses in entries:
            # Bound both entries and retained parsed header evidence. Overflow
            # only disables reuse, never excludes an ambiguous message.
            if key is None or len(addresses) > 32 or any(len(a) > 254 for a in addresses):
                continue
            _HEADER_CACHE[key] = (now + _HEADER_TTL, frozenset(addresses))
            _HEADER_CACHE.move_to_end(key)
        while len(_HEADER_CACHE) > _HEADER_LIMIT:
            _HEADER_CACHE.popitem(last=False)
            _DIAG_COUNTS["cache_evictions"] += 1


class ProviderReadError(ValueError):
    """Short public reason only; never include provider bodies or credentials."""


def message_dto(value):
    raw = base64.b64decode(
        value["raw"] + "=" * (-len(value["raw"]) % 4), altchars=b"-_", validate=True
    )
    msg = BytesParser(policy=email.policy.default).parsebytes(raw)
    if msg.defects:
        raise ProviderReadError("invalid_provider_mime")
    headers = {}
    for name, val in msg.items():
        key = name.lower()
        if key in headers and key in {
            "from",
            "to",
            "cc",
            "subject",
            "message-id",
            "x-crm-obligation",
        }:
            raise ProviderReadError("ambiguous_provider_headers")
        headers[key] = str(val)
    bodies = {"plain": [], "html": []}
    attachments = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        if part.get_content_disposition() == "attachment" or part.get_filename():
            attachments.append(
                {
                    "name": part.get_filename(),
                    "content_type": part.get_content_type(),
                    "size": len(part.get_payload(decode=True) or b""),
                }
            )
        elif part.get_content_type() in ("text/plain", "text/html"):
            bodies[part.get_content_subtype()].append(part.get_content())
    return {
        "id": value["id"],
        "thread_id": value["threadId"],
        "labels": sorted(value["labelIds"]),
        "sent_at": datetime.fromtimestamp(
            int(value["internalDate"]) / 1000, UTC
        ).isoformat().replace("+00:00", "Z"),
        "internal_date_ms": int(value["internalDate"]),
        "headers": headers,
        "from": [a for _, a in getaddresses([headers.get("from", "")])],
        "to": [a for _, a in getaddresses([headers.get("to", "")])],
        "cc": [a for _, a in getaddresses([headers["cc"]])]
        if headers.get("cc")
        else [],
        "plain": "\n".join(bodies["plain"]),
        "html": "\n".join(bodies["html"]),
        "attachments": attachments,
    }


class GmailProvider:
    """Fresh reads plus exactly one HTTP POST. Only service intent owns send.

    No auth-refresh replay, adapter retries or redirects on the POST. Credentials
    are opened lazily; importing the service/collectors has no provider effects.
    """

    def __init__(self, *, credentials_file, session=None):
        self.credentials_file = credentials_file
        self.session = session
        if session is not None:
            self._disable_retries()

    def _disable_retries(self):
        from requests.adapters import HTTPAdapter

        for scheme in ("http://", "https://"):
            self.session.mount(scheme, HTTPAdapter(max_retries=0))

    def _authorized(self):
        if self.session is None:
            from google.auth.transport.requests import AuthorizedSession

            from src.crm.google_credentials import load_google_credentials

            generation = credential_generation(self.credentials_file)
            scopes = (
                "https://www.googleapis.com/auth/gmail.modify",
                "https://www.googleapis.com/auth/gmail.settings.basic",
            )
            credentials = load_google_credentials(
                self.credentials_file,
                list(scopes),
            )
            self.session = AuthorizedSession(credentials, max_refresh_attempts=0)
            if generation is not None and credential_generation(self.credentials_file) == generation:
                self._gmail_cache_identity = (generation, scopes)
                self._gmail_cache_session = self.session
            self._disable_retries()
        return self.session

    def get(self, path, params=None):
        if not path.startswith("/") or ".." in path or "?" in path or "#" in path:
            raise ProviderReadError("invalid_provider_path")
        check_cooldown()
        # Retry a transient fresh GET inside prepare before failing the claim.
        # Retry only this read, once. Never retry quota/auth failures or any POST.
        from requests.exceptions import ConnectionError, Timeout
        for attempt in range(2):
            try:
                _count_get(self, _family(path, params))
                response = self._authorized().get(
                    "https://gmail.googleapis.com/gmail/v1/users/me" + path,
                    params=params,
                    timeout=20,
                    allow_redirects=False,
                )
            except (ConnectionError, Timeout):
                if attempt == 0:
                    check_cooldown()
                    continue
                raise ProviderReadError("provider_read_failed") from None
            try:
                observe_quota(response)  # quota always stops, without a retry.
            except GmailRateLimited as exc:
                _private_event(self, "google_429", family=_family(path, params), receipt=exc.provider_receipt)
                raise
            if response.status_code == 200:
                return response.json()
            if response.status_code in (502, 503, 504) and attempt == 0:
                check_cooldown()
                continue
            raise ProviderReadError("provider_read_failed")

    def send(self, raw, thread_id):
        check_cooldown()
        response = self._authorized().post(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
            json={"raw": base64.urlsafe_b64encode(raw).decode(), "threadId": thread_id},
            timeout=20,
            allow_redirects=False,
        )
        observe_quota(response)
        if response.status_code != 200:
            raise ProviderReadError("provider_send_unconfirmed")
        # HTTP acknowledgement is never a SENT receipt; the service rereads.
        return response.json()


def decode_payload(encoded):
    try:
        raw = base64.b64decode(encoded, validate=True)
        if not 1 <= len(raw) <= 256 * 1024 or base64.b64encode(raw).decode() != encoded:
            raise ValueError
        return raw
    except (ValueError, TypeError):
        raise ValueError("invalid_payload_base64") from None


def payload_dto(raw, *, thread_id):
    return message_dto(
        {
            "id": "candidate",
            "threadId": thread_id,
            "labelIds": [],
            "internalDate": "0",
            "raw": base64.urlsafe_b64encode(raw).decode(),
        }
    )


def validate_payload(encoded, context, obligation, anchor):
    raw = decode_payload(encoded)
    msg = BytesParser(policy=email.policy.default).parsebytes(raw)
    candidate = payload_dto(raw, thread_id=obligation["thread_id"])
    signature = context["provider"]["send_as"]["signature"]
    alias = context["provider"]["send_as"]
    # Gmail may omit the optional display name or return an empty string.
    # Preserve that exact identity; never invent a name or relax the mailbox.
    expected_from = alias.get("displayName", "")
    from email.utils import parseaddr

    name, address = parseaddr(str(msg.get("From", "")))
    if (
        type(expected_from) is not str
        or name != expected_from
        or address != obligation["mailbox"]
        or candidate["to"] != anchor["to"]
        or candidate["cc"] != anchor["cc"]
        or msg.get("Bcc") is not None
        or msg.get("Sender") is not None
        or msg.get("Reply-To") is not None
        or msg.get_content_type() != "multipart/alternative"
        or msg.defects
        or candidate["attachments"]
        or not candidate["plain"].strip()
        or not candidate["html"].strip()
    ):
        raise ValueError("invalid_followup_mime")
    expected_references = (
        anchor["headers"].get("references", "") + " " + anchor["headers"]["message-id"]
    ).strip()
    for key, expected in {
        "subject": anchor["headers"]["subject"],
        "in-reply-to": anchor["headers"]["message-id"],
        "references": expected_references,
        "x-crm-obligation": obligation["key"],
    }.items():
        actual = str(msg.get(key, ""))
        # RFC folding adds field whitespace before a long opaque marker; remove
        # only surrounding field whitespace, never change the marker itself.
        if key in {"x-crm-obligation", "in-reply-to"}:
            actual = actual.strip(" \t\r\n")
            if key == "in-reply-to":
                expected = expected.strip(" \t\r\n")
        elif key == "references":
            # Compare the ordered opaque Message-ID tokens, not legal RFC
            # folding whitespace inserted by the MIME serializer.
            actual, expected = " ".join(actual.split()), " ".join(expected.split())
        if len(msg.get_all(key, [])) != 1 or actual != expected:
            raise ValueError("followup_headers_changed")
    parts = list(msg.iter_parts())
    if (
        len(parts) != 2
        or [p.get_content_type() for p in parts] != ["text/plain", "text/html"]
        or any(p.get_content_charset() != "utf-8" or p.defects for p in parts)
    ):
        raise ValueError("invalid_alternative_parts")
    html = candidate["html"].rstrip("\r\n")
    prefix = '<div style="font-family:Verdana,sans-serif">'
    # The trusted executor emits this exact Gmail signature envelope. Accept
    # only that fixed grammar or the original unwrapped contract; never locate
    # a signature by substring or relax its exact bytes, font or body safety.
    envelope = (
        '<span class="gmail_signature_prefix">-- </span><br>'
        '<div class="gmail_signature" data-smartmail="gmail_signature">'
    )
    wrapped_suffix = envelope + signature + "</div></div>"
    legacy_suffix = signature + "</div>"
    suffix = wrapped_suffix if html.endswith(wrapped_suffix) else legacy_suffix
    if not html.startswith(prefix) or not html.endswith(suffix):
        raise ValueError("signature_or_font_changed")
    from src.crm.connectors.note_drafts import safe_body_html

    safe_body_html(html[len(prefix) : -len(suffix)])
    return raw


def receipt_matches(message, raw, obligation, reserved_at):
    expected = payload_dto(raw, thread_id=obligation["thread_id"])
    fields = ("from", "to", "cc", "plain", "html")
    headers = (
        "from",
        "to",
        "cc",
        "subject",
        "in-reply-to",
        "references",
        "x-crm-obligation",
    )
    return (
        "SENT" in message["labels"]
        and "DRAFT" not in message["labels"]
        and message["thread_id"] == obligation["thread_id"]
        and not message["attachments"]
        and datetime.fromisoformat(message["sent_at"].replace("Z", "+00:00"))
        >= reserved_at.replace(microsecond=0)
        and all(message[k] == expected[k] for k in fields)
        and all(
            message["headers"].get(k, "") == expected["headers"].get(k, "")
            for k in headers
        )
    )


def pages(provider, path, field, params, *, deadline):
    cursor = None
    seen = set()
    while True:
        if time.monotonic() >= deadline:
            raise ProviderReadError("provider_deadline")
        page = provider.get(path, params | ({"pageToken": cursor} if cursor else {}))
        rows = page.get(field, [])
        if not isinstance(rows, list):
            raise ProviderReadError("provider_page_invalid")
        yield from rows
        cursor = page.get("nextPageToken")
        if cursor is None:
            return
        if not isinstance(cursor, str) or not cursor:
            raise ProviderReadError("provider_page_invalid")
        if cursor in seen:
            raise ProviderReadError("provider_cursor_repeated")
        seen.add(cursor)


def recent_mailbox_sent_at(provider, mailbox, now, *, budget_seconds=90):
    """Fresh mailbox-wide SENT timestamps, including manual/unrelated sends.

    Query is only an index aid: verify labels/identity/time on every direct GET.
    Missing pages, changed history or malformed timestamps are not permission.
    """
    from datetime import timedelta

    deadline = time.monotonic() + budget_seconds
    profile = provider.get("/profile")
    if profile.get("emailAddress") != mailbox or not profile.get("historyId"):
        raise ProviderReadError("mailbox_mismatch")
    latest = None
    lower_bound = int((now - timedelta(seconds=600)).timestamp()) - 1
    for ref in pages(provider, "/messages", "messages", {
        "q": f"in:sent after:{lower_bound}", "labelIds": "SENT",
        "maxResults": 100, "includeSpamTrash": "true",
    }, deadline=deadline):
        message = provider.get("/messages/" + quote(ref["id"], safe=""), {"format": "metadata"})
        if message.get("id") != ref["id"] or message.get("threadId") != ref["threadId"]:
            raise ProviderReadError("sent_identity_changed")
        if "SENT" not in message.get("labelIds", []) or "DRAFT" in message["labelIds"]:
            raise ProviderReadError("sent_labels_changed")
        try:
            instant = datetime.fromtimestamp(int(message["internalDate"]) / 1000, UTC)
        except (KeyError, TypeError, ValueError, OverflowError):
            raise ProviderReadError("sent_timestamp_unavailable") from None
        latest = max(latest, instant) if latest is not None else instant
    after = provider.get("/profile")
    if after.get("emailAddress") != mailbox or after.get("historyId") != profile["historyId"]:
        raise ProviderReadError("mailbox_changed_during_spacing_read")
    return latest


def draft_inventory(provider, *, deadline):
    """Fresh, unfiltered membership; Gmail replaces message IDs on draft edit."""
    inventory, message_ids = {}, set()
    for ref in pages(provider, "/drafts", "drafts", {
        "maxResults": 500, "includeSpamTrash": "true",
    }, deadline=deadline):
        draft_id = ref["id"]
        message_id, thread_id = ref["message"]["id"], ref["message"]["threadId"]
        if (
            any(type(v) is not str or not v for v in (draft_id, message_id, thread_id))
            or draft_id in inventory or message_id in message_ids
        ):
            raise ProviderReadError("draft_identity_conflict")
        inventory[draft_id] = (message_id, thread_id)
        message_ids.add(message_id)
    return inventory


def metadata_addresses(value):
    """Conservative exclusion only. Anything malformed/ambiguous requires RAW.

    No subject/domain heuristics. Use the same address parser as message_dto;
    require well-formed address headers before excluding any draft by metadata.
    """
    try:
        # Malformed metadata is never a negative relevance answer.
        datetime.fromtimestamp(int(value["internalDate"]) / 1000, UTC)
        headers = {}
        critical = {"from", "to", "cc", "subject", "message-id", "x-crm-obligation"}
        for item in value["payload"]["headers"]:
            name, text = item["name"], item["value"]
            if type(name) is not str or type(text) is not str:
                return None
            key = name.lower()
            if not name or any(not 33 <= ord(c) <= 126 or c == ":" for c in name) or any(c in text for c in "\r\n"):
                return None
            if key in critical and key in headers:
                return None
            headers[key] = text
        if not headers.get("from") or not headers.get("to"):
            return None
        addresses = set()
        for key in ("from", "to", "cc"):
            if key not in headers:
                continue
            header = email.policy.default.header_factory(key, headers[key])
            if header.defects:
                return None
            parsed = getaddresses([str(header)])
            if not parsed:
                return None
            for _, address in parsed:
                valid_address(address)
                addresses.add(address)
        return addresses
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError, OSError):
        return None


def read_context(provider, mailbox, recipients, *, thread_ids=(), budget_seconds=90):
    start = time.monotonic()
    provider._private_read_stats = {"get_attempts": 0, "endpoint_counts": {},
                                    "draft_inventory_count": None}
    outcome = "failed"
    try:
        result = _read_context(provider, mailbox, recipients, thread_ids=thread_ids,
                               budget_seconds=budget_seconds)
        outcome = "complete"
        return result
    except GmailRateLimited:
        outcome = "quota_or_shared_hold"
        raise
    finally:
        provider._private_read_stats["elapsed_seconds"] = round(time.monotonic() - start, 3)
        _private_event(provider, outcome)


def _message_state(value, message_id, thread_id):
    """All mutable DTO state must be supplied by this read, never the cache."""
    if value.get("id") != message_id or value.get("threadId") != thread_id:
        raise ProviderReadError("message_identity_conflict")
    labels = value.get("labelIds")
    if not isinstance(labels, list) or any(type(label) is not str for label in labels):
        raise ProviderReadError("message_labels_unavailable")
    instant = int(value["internalDate"])
    return {"id": message_id, "thread_id": thread_id, "labels": sorted(labels),
            "internal_date_ms": instant,
            "sent_at": datetime.fromtimestamp(instant / 1000, UTC).isoformat().replace("+00:00", "Z")}


def _read_context(provider, mailbox, recipients, *, thread_ids=(), budget_seconds=90):
    """Validated immutable MIME plus fresh state, stable complete membership."""
    deadline = time.monotonic() + budget_seconds
    try:
        valid_address(mailbox)
        recipients = sorted(set(recipients))
        if not recipients:
            raise ProviderReadError("recipient_mapping_missing")
        for address in recipients:
            valid_address(address)
        profile = provider.get("/profile")
        if profile["emailAddress"] != mailbox:
            raise ProviderReadError("mailbox_mismatch")
        scope = cache_scope(provider, mailbox)
        aliases = provider.get("/settings/sendAs").get("sendAs", [])
        aliases = [a for a in aliases if a.get("sendAsEmail") == mailbox]
        if len(aliases) != 1 or not aliases[0].get("signature"):
            raise ProviderReadError("send_as_unavailable")
        threads = set(thread_ids)
        for recipient in recipients:
            query = "{from:" + recipient + " to:" + recipient + " cc:" + recipient + "}"
            for ref in pages(
                provider,
                "/messages",
                "messages",
                {"q": query, "maxResults": 100, "includeSpamTrash": "true"},
                deadline=deadline,
            ):
                threads.add(ref["threadId"])
        values = []
        # Request-local DTOs include current labels. The process cache below
        # holds only validated immutable content, never these state fields.
        raw_messages = {}

        def raw_message(message_id, thread_id, fresh=None):
            if time.monotonic() >= deadline:
                raise ProviderReadError("provider_deadline")
            message = raw_messages.get(message_id)
            if message is None:
                key = evidence_key(cache_scope(provider, mailbox), message_id)
                content = _CONTENT_CACHE.get(key)
                if content is None:
                    value = provider.get("/messages/" + quote(message_id, safe=""), {"format": "raw"})
                    state = _message_state(value, message_id, thread_id)
                    message = message_dto(value)
                    content = {k: v for k, v in message.items() if k not in state}
                    content["thread_id"] = thread_id
                    _CONTENT_CACHE.put(key, content)
                else:
                    if content["thread_id"] != thread_id:
                        raise ProviderReadError("message_identity_conflict")
                    # MINIMAL officially promises only IDs/labels, not date.
                    # Reuse a reference only when it actually has all fields.
                    if fresh is None or not all(k in fresh for k in ("id", "threadId", "labelIds", "internalDate")):
                        fresh = provider.get("/messages/" + quote(message_id, safe=""), {"format": "metadata"})
                    state = _message_state(fresh, message_id, thread_id)
                message = content | state
            if message["id"] != message_id or message["thread_id"] != thread_id:
                raise ProviderReadError("message_identity_conflict")
            # Only index references to thread messages already required in values.
            # Ambiguous/unrelated draft bodies are not retained as reusable state.
            if thread_id in threads:
                raw_messages[message_id] = message
            return message

        for thread_id in sorted(threads):
            if time.monotonic() >= deadline:
                raise ProviderReadError("provider_deadline")
            thread = provider.get(
                "/threads/" + quote(thread_id, safe=""), {"format": "minimal"}
            )
            if thread.get("id") != thread_id or not thread.get("messages"):
                raise ProviderReadError("thread_incomplete")
            messages = {}
            for ref in thread["messages"]:
                message = raw_message(ref["id"], thread_id, fresh=ref)
                if message["id"] in messages:
                    raise ProviderReadError("message_identity_conflict")
                messages[message["id"]] = message
            values.append(
                {"id": thread_id, "messages": [messages[k] for k in sorted(messages)]}
            )
        drafts = []
        inventory = draft_inventory(provider, deadline=deadline)
        provider._private_read_stats["draft_inventory_count"] = len(inventory)
        for draft_id, (message_id, thread_id) in sorted(inventory.items()):
            if time.monotonic() >= deadline:
                raise ProviderReadError("provider_deadline")
            metadata = None
            if message_id not in raw_messages and thread_id not in threads:
                cache_key = _header_key(provider, mailbox, message_id)
                addresses = _cached_addresses(cache_key)
                # Current complete direct inventory supplies the DRAFT contract,
                # NOT a cached label. Google destroys/replaces message IDs on
                # draft edit/send. Recheck full tuples and profile below.
                if addresses is not None and not (addresses - {mailbox}).intersection(recipients):
                    continue
                metadata = provider.get(
                    "/messages/" + quote(message_id, safe=""), {"format": "metadata"}
                )
                if metadata.get("id") != message_id or metadata.get("threadId") != thread_id:
                    raise ProviderReadError("draft_identity_conflict")
                labels = metadata.get("labelIds")
                if not isinstance(labels, list) or any(type(v) is not str for v in labels) or "DRAFT" not in labels or "SENT" in labels:
                    raise ProviderReadError("draft_labels_changed")
                addresses = metadata_addresses(metadata)
                if addresses is not None:
                    # Immutable header evidence survives an interrupted read.
                    # This never publishes a context or reusable labels. A later
                    # read still verifies profile, current full inventory and
                    # exact message ID before using it only for exclusion.
                    _remember_addresses([(cache_key, addresses)])
                if addresses is not None and not (addresses - {mailbox}).intersection(recipients):
                    continue
            message = raw_message(message_id, thread_id, fresh=metadata)
            if "DRAFT" not in message["labels"] or "SENT" in message["labels"]:
                raise ProviderReadError("draft_labels_changed")
            # Preserve exact existing scope: (From + To + CC) minus mailbox,
            # intersect recipients OR discovered/explicit thread membership.
            addresses = set(message["to"] + message["cc"] + message["from"]) - {mailbox}
            if addresses.intersection(recipients) or message["thread_id"] in threads:
                drafts.append({"id": draft_id, "message": message})
        # historyId alone is not a draft freshness token. Re-list every ID and
        # embedded message/thread pair; edits replace the exact message ID.
        if draft_inventory(provider, deadline=deadline) != inventory:
            raise ProviderReadError("draft_inventory_changed")
        after = provider.get("/profile")
        if (
            after["emailAddress"] != mailbox
            or after["historyId"] != profile["historyId"]
        ):
            raise ProviderReadError("provider_history_changed")
        if scope is not None and cache_scope(provider, mailbox) != scope:
            raise ProviderReadError("provider_authorization_changed")
        return {
            "complete": True,
            "provider_history_id": profile["historyId"],
            "provider": {
                "mailbox": mailbox,
                "threads": values,
                "direct_drafts": sorted(drafts, key=lambda d: d["id"]),
                "send_as": aliases[0],
            },
        }
    except (ProviderReadError, GmailRateLimited):
        raise
    except Exception:  # noqa: BLE001 -- sanitize any provider exception at the privacy boundary
        raise ProviderReadError("provider_read_failed") from None
