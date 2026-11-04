"""Shared, durable cooldown for this installation's configured commercial mailbox.

Only a deadline and allowlisted provider status are persisted; no mailbox content.
This never retries a request and never changes a business obligation.
"""
import contextlib
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import fcntl
import json
import math
import os
from pathlib import Path
import re
import random
import tempfile
import time


_REASONS = frozenset({
    'rateLimitExceeded', 'userRateLimitExceeded', 'quotaExceeded',
    'dailyLimitExceeded', 'dailyLimitExceededUnreg', 'backendError',
})


# Deliberately finite vocabulary, not an arbitrary string/URL sink. Unknown
# metrics/limits are omitted until independently reviewed, never guessed.
_QUOTA_VALUES = {
    'quota_metric': frozenset('gmail.googleapis.com/' + name for name in
                             ('default', 'quota_units', 'requests', 'read_requests')),
    'quota_limit': frozenset({'PerUserPerMinute', 'PerProjectPerMinute', 'PerProjectPerDay',
                             'RequestsPerMinutePerUser', 'RequestsPerMinutePerProject',
                             'DefaultPerMinutePerUser', 'DefaultPerMinutePerProject'}),
    'quota_unit': frozenset({'1/min/{project}', '1/min/{project}/{user}',
                            '1/d/{project}', '1/d/{project}/{user}'}),
}


def _safe_quota_fields(value):
    if not isinstance(value, dict):
        return {}
    safe = {key: value[key] for key, allowed in _QUOTA_VALUES.items()
            if type(value.get(key)) is str and value[key] in allowed}
    number = value.get('limit_value')
    if type(number) is int and 0 <= number <= 2**63 - 1:
        safe['limit_value'] = number
    elif type(number) is str and re.fullmatch(r'[0-9]{1,19}', number) and int(number) <= 2**63 - 1:
        safe['limit_value'] = int(number)
    return safe


def _quota_details(details):
    output = []
    if not isinstance(details, list):
        return output
    for detail in details[:16]:
        if not isinstance(detail, dict):
            continue
        kind = detail.get('@type')
        candidates = []
        if kind == 'type.googleapis.com/google.rpc.ErrorInfo':
            if detail.get('domain') in ('googleapis.com', 'gmail.googleapis.com') and detail.get('reason') in (
                    'RATE_LIMIT_EXCEEDED', 'QUOTA_EXCEEDED', 'RESOURCE_EXHAUSTED'):
                metadata = detail.get('metadata')
                if isinstance(metadata, dict):
                    candidates = [dict(metadata, limit_value=metadata.get('quota_limit_value', metadata.get('limit_value')))]
        elif kind == 'type.googleapis.com/google.rpc.QuotaFailure':
            violations = detail.get('violations')
            if isinstance(violations, list):
                candidates = [{'quota_metric': v.get('quotaMetric'), 'quota_limit': v.get('quotaId', v.get('quotaLimit')),
                               'limit_value': v.get('quotaValue'), 'quota_unit': v.get('quotaUnit')}
                              for v in violations[:16] if isinstance(v, dict) and v.get('apiService') == 'gmail.googleapis.com']
        for candidate in candidates:
            safe = _safe_quota_fields(candidate)
            if safe and safe not in output:
                output.append(safe)
    return output[:16]


def _safe_receipt(value):
    """Never retain provider messages, arbitrary reasons, URLs or header text."""
    safe = {}
    if not isinstance(value, dict):
        return safe
    if value.get('http_status') == 429:
        safe['http_status'] = 429
    status = value.get('error_status')
    if status in ('RESOURCE_EXHAUSTED', 'PERMISSION_DENIED', 'UNAUTHENTICATED', 'INTERNAL', 'UNAVAILABLE'):
        safe['error_status'] = status
    for key in ('google_details_present', 'google_quota_message_present'):
        if type(value.get(key)) is bool:
            safe[key] = value[key]
    code = value.get('error_code')
    if type(code) is int and 100 <= code <= 599:
        safe['error_code'] = code
    reasons = value.get('error_reasons')
    if isinstance(reasons, list):
        safe['error_reasons'] = sorted({r for r in reasons if type(r) is str and r in _REASONS})
    retry = value.get('retry_after')
    if isinstance(retry, str) and 0 < len(retry) <= 128 and '\r' not in retry and '\n' not in retry:
        try:
            if retry.isascii() and retry.isdigit():
                int(retry)
            else:
                instant = parsedate_to_datetime(retry)
                if instant.strftime('%a, %d %b %Y %H:%M:%S GMT') != retry:
                    raise ValueError
            safe['retry_after'] = retry
        except (TypeError, ValueError, OverflowError):
            pass
    details = value.get('quota_details')
    if isinstance(details, list):
        clean = [_safe_quota_fields(item) for item in details[:16]]
        clean = [item for item in clean if item]
        if clean:
            safe['quota_details'] = clean
    return safe


class GmailRateLimited(ValueError):
    reason_code = 'provider_rate_limited'

    def __init__(self, retry_at, provider_receipt=None):
        super().__init__(self.reason_code)
        self.retry_at = float(retry_at)
        self.retry_after = datetime.fromtimestamp(self.retry_at, timezone.utc).isoformat()
        self.provider_receipt = _safe_receipt(provider_receipt)

    @property
    def headers(self):
        headers = {'Retry-After': str(max(1, math.ceil(self.retry_at - time.time()))),
                   'X-Provider-Retry-At': self.retry_after}
        # Routers already forward exc.headers. Separate original Google evidence
        # from the locally generated cooldown Retry-After.
        for key, header in (
            ('http_status', 'X-Google-HTTP-Status'),
            ('error_code', 'X-Google-Error-Code'),
            ('retry_after', 'X-Google-Retry-After'),
        ):
            if key in self.provider_receipt:
                headers[header] = str(self.provider_receipt[key])
        if self.provider_receipt.get('error_reasons'):
            headers['X-Google-Error-Reasons'] = ','.join(self.provider_receipt['error_reasons'])
        if self.provider_receipt.get('quota_details'):
            headers['X-Google-Quota-Details'] = json.dumps(self.provider_receipt['quota_details'], separators=(',', ':'))
        return headers


def _path():
    override = os.environ.get('CRM_GMAIL_COOLDOWN_FILE')
    if override:
        return Path(override)
    journal = os.environ.get('CRM_NOTE_DRAFT_JOURNAL_DIR')
    if journal:
        return Path(journal) / 'gmail-provider-cooldown.json'
    return Path.home() / '.hermes/ops/head-of-sales/gmail-provider-cooldown.json'


def _active_cooldown():
    # One atomic file read binds the stored receipt to its deadline.
    try:
        state = json.loads(_path().read_text())
        value = float(state['retry_at'])
        if math.isfinite(value) and value > time.time():
            return value, state.get('provider_response')
    except (FileNotFoundError, ValueError, TypeError, KeyError):
        pass
    return None, None


def retry_at():
    return _active_cooldown()[0]


def check():
    until, receipt = _active_cooldown()
    if until is not None:
        raise GmailRateLimited(until, receipt)


def record(until, provider_receipt=None, *, observed_at=None, provider_source=None):
    """Atomic future-429 streak; legacy callers may still record a deadline.

    No success clears state. After an hour without another actual 429, the next
    streak starts at one. No timer, scheduler change or request retry is added.
    """
    now = time.time() if observed_at is None else observed_at
    until = float(until) if until is not None else None
    if observed_at is None and (until is None or not math.isfinite(until) or until <= now):
        return
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.with_suffix('.lock').open('a') as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            previous = json.loads(path.read_text())
            if not isinstance(previous, dict):
                previous = {}
        except (FileNotFoundError, ValueError):
            previous = {}
        previous_until, previous_receipt = _active_cooldown()
        policy = {}
        if observed_at is not None:
            last, attempt = previous.get('last_429_at'), previous.get('backoff_attempt')
            # Concurrent responses can acquire this lock out of arrival order.
            # Keep the persisted clock nondecreasing; do not reset the streak
            # merely because an older response finishes recording later.
            recent = type(last) in (int, float) and math.isfinite(last) and abs(now - last) <= 3600
            if recent:
                now = max(now, last)
            attempt = min(attempt + 1, 7) if recent and type(attempt) is int and 1 <= attempt <= 7 else 1
            delay = min(2 ** (attempt - 1) + random.SystemRandom().uniform(0, 1), 64)
            fallback = now + delay
            provider_until = until
            until = max(fallback, until or 0)
            policy = {'last_429_at': now, 'backoff_attempt': attempt,
                      'fallback_retry_at': fallback, 'deadline_source': 'local_exponential_jitter'}
            if provider_until is not None:
                policy.update(provider_retry_at=provider_until, provider_retry_source=provider_source)
                if provider_until >= fallback:
                    policy['deadline_source'] = provider_source
            if previous_until is not None and previous_until > until:
                policy['deadline_source'] = 'preserved_existing_deadline'
                old_source = previous.get('preserved_deadline_source') if previous.get('deadline_source') == 'preserved_existing_deadline' else previous.get('deadline_source')
                policy['preserved_deadline_source'] = old_source if old_source in (
                    'google_retry_after_header', 'google_message_retry_date', 'google_retry_info',
                    'local_exponential_jitter') else 'legacy_unknown'
                policy['preserved_retry_at'] = previous_until
        else:
            # Keep bounded local policy provenance for legacy record callers.
            for key in ('last_429_at', 'backoff_attempt', 'fallback_retry_at', 'deadline_source',
                        'provider_retry_at', 'provider_retry_source', 'preserved_retry_at', 'preserved_deadline_source'):
                if key in previous:
                    policy[key] = previous[key]
        until = max(until, previous_until or 0)
        if provider_receipt is None:
            provider_receipt = previous_receipt
        fd, name = tempfile.mkstemp(dir=path.parent, prefix='.gmail-cooldown-')
        try:
            with os.fdopen(fd, 'w') as output:
                value = {'reason': 'provider_rate_limited', 'retry_at': until, **policy}
                if provider_receipt is not None:
                    value['provider_response'] = _safe_receipt(provider_receipt)
                json.dump(value, output)
            os.replace(name, path)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(name)
        return until, _safe_receipt(provider_receipt)


def parse_retry_after(value, *, now=None):
    now = time.time() if now is None else now
    if not isinstance(value, str):
        return None
    try:
        until = now + int(value) if value.strip().isdigit() else parsedate_to_datetime(value).timestamp()
        return until if math.isfinite(until) and until > now else None
    except (ValueError, TypeError, OverflowError):
        return None


def observe(response):
    if response.status_code != 429:
        return
    now = time.time()
    receipt = {'http_status': 429, 'retry_after': response.headers.get('Retry-After')}
    try:
        error = response.json().get('error', {})
        if not isinstance(error, dict):
            error = {}
    except (ValueError, TypeError, AttributeError):
        error = {}
    receipt['error_status'] = error.get('status')
    receipt['google_details_present'] = bool(error.get('details'))
    receipt['google_quota_message_present'] = type(error.get('message')) is str and 'Quota exceeded for quota metric ' in error['message'][:2000]
    receipt['error_code'] = error.get('code')
    errors = error.get('errors')
    receipt['error_reasons'] = [item.get('reason') for item in errors if isinstance(item, dict)] if isinstance(errors, list) else []
    receipt['quota_details'] = _quota_details(error.get('details'))
    # Gmail sometimes reports only its documented quota sentence. Accept the
    # fixed sentence grammar and the existing finite metric/limit vocabulary.
    # No arbitrary message text, project, consumer, subject or description.
    message = error.get('message')
    if type(message) is str:
        match = re.search(r"Quota exceeded for quota metric '([^'\n]{1,100})' and limit '([^'\n]{1,100})'", message[:2000])
        if match:
            item = _safe_quota_fields({'quota_metric': match[1], 'quota_limit': match[2]})
            if item and item not in receipt['quota_details']:
                receipt['quota_details'].append(item)
    receipt = _safe_receipt(receipt)
    candidates = []
    # Parse only the sanitized original header; local forwarded Retry-After
    # compatibility remains in parse_retry_after, which is unchanged.
    header_until = parse_retry_after(receipt.get('retry_after'), now=now)
    if header_until is not None:
        candidates.append((header_until, 'google_retry_after_header'))
    # Compatibility with existing Gmail ISO-date-in-message evidence. Never
    # persist the message. Compare ALL sources, not first-header-wins.
    message = error.get('message', '')
    if isinstance(message, str):
        for match in re.finditer(r'Retry after ([0-9]{4}-[0-9T:.+-]+Z)', message[:2000]):
            try:
                instant = datetime.fromisoformat(match[1].replace('Z', '+00:00')).timestamp()
                if math.isfinite(instant) and instant > now:
                    candidates.append((instant, 'google_message_retry_date'))
            except (ValueError, TypeError, OverflowError, OSError):
                continue
    details = error.get('details')
    if isinstance(details, list):
        for detail in details[:16]:
            if not isinstance(detail, dict) or detail.get('@type') != 'type.googleapis.com/google.rpc.RetryInfo':
                continue
            delay = detail.get('retryDelay')
            if type(delay) is str and re.fullmatch(r'[0-9]{1,12}(?:\.[0-9]{1,9})?s', delay):
                instant = now + float(delay[:-1])
                if now < instant < 253402300799:
                    candidates.append((instant, 'google_retry_info'))
    # Validate datetime representability; an untrusted numeric header must not
    # turn the sanitized exception path into overflow or an invalid state.
    candidates = [(until, source) for until, source in candidates if until < 253402300799]
    until, source = max(candidates) if candidates else (None, None)
    until, receipt = record(until, receipt, observed_at=now, provider_source=source)
    raise GmailRateLimited(until, receipt)
