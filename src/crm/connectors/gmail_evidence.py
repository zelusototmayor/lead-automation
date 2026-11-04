"""Private process-local immutable Gmail evidence, never mailbox/action state."""
from collections import OrderedDict
import json
from pathlib import Path
from threading import Lock
import time


def credential_generation(filename):
    """File identity/generation only; never read or retain credential contents."""
    try:
        path = Path(filename).resolve(strict=True)
        stat = path.stat()
        if not path.is_file():
            return None
        return (str(path), stat.st_dev, stat.st_ino, stat.st_mtime_ns,
                stat.st_ctime_ns, stat.st_size)
    except (OSError, TypeError, ValueError):
        return None


def cache_scope(provider, mailbox):
    """Bind a verified mailbox to the generation actually loaded by this session.

    Unknown/injected sessions and a changed file/session disable reuse. Requested
    scopes distinguish the two connector authorization contexts. Token refresh
    doesn't change the underlying file grant; rotation/replacement does.
    """
    identity = getattr(provider, '_gmail_cache_identity', None)
    if (identity is None or identity[0] is None
            or type(mailbox) is not str or not mailbox
            or provider.session is not getattr(provider, '_gmail_cache_session', None)
            or credential_generation(provider.credentials_file) != identity[0]):
        return None
    return (mailbox, identity)


def evidence_key(scope, message_id):
    if scope is None or type(message_id) is not str or not 1 <= len(message_id) <= 256:
        return None
    return (scope, message_id)


class ImmutableEvidenceCache:
    """TTL/LRU with exact retained-payload byte and entry bounds, copy on read.

    Callers validate evidence before put and supply fresh state after get. JSON
    bytes prevent a returned DTO from mutating retained evidence. No disk/logs.
    """
    def __init__(self, *, ttl=900, limit=256, byte_limit=4*1024*1024):
        self.ttl, self.limit, self.byte_limit = ttl, limit, byte_limit
        self._entries = OrderedDict()
        self._bytes = 0
        self._lock = Lock()

    def clear(self):
        with self._lock:
            self._entries.clear()
            self._bytes = 0

    def get(self, key):
        if key is None:
            return None
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            expires, payload = entry
            if expires <= time.monotonic():
                self._entries.pop(key)
                self._bytes -= len(payload)
                return None
            self._entries.move_to_end(key)
        return json.loads(payload)

    def put(self, key, value):
        if key is None:
            return
        payload = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()
        # Oversized content simply remains uncached, without weakening coverage.
        if len(payload) > min(self.byte_limit, 256*1024):
            return
        now = time.monotonic()
        with self._lock:
            old = self._entries.pop(key, None)
            if old is not None:
                self._bytes -= len(old[1])
            self._entries[key] = (now + self.ttl, payload)
            self._bytes += len(payload)
            while self._entries and (len(self._entries) > self.limit or self._bytes > self.byte_limit):
                _, (_, evicted) = self._entries.popitem(last=False)
                self._bytes -= len(evicted)
