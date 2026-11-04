"""Private resumable ID cache, not business state or send authority.

The existing durable journal mount carries one exact-ID list per scan. Signed
cursors bind an immutable prefix, so retrying a page after a lost response and
restarting the process are safe. No growing reference list travels in a URL.
"""
import fcntl
import os
import stat
import tempfile
from pathlib import Path
from uuid import UUID

from src.crm.services.followup_context import digest
from src.crm.services.followup_review import FollowupError, canonical, strict_json

MAX_BYTES = 16 * 1024 * 1024


def _path(state):
    try:
        scan_id = state['scan_id']
        if UUID(scan_id).hex != scan_id:
            raise ValueError
        root = Path(os.environ['CRM_NOTE_DRAFT_JOURNAL_DIR'])
        if not root.is_absolute() or root.is_symlink() or not root.is_dir():
            raise ValueError
        root = root / 'followup-inventory'
        root.mkdir(mode=0o700, exist_ok=True)
        info = root.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.geteuid():
            raise ValueError
        return root / (scan_id + '.json')
    except (KeyError, ValueError, OSError, TypeError):
        raise FollowupError('inventory_checkpoint_unavailable', 503) from None


def _read(path, state):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.geteuid():
            raise ValueError
        data = strict_json(stream.read(MAX_BYTES + 1), maximum=MAX_BYTES)
    if any(data.get(k) != state[k] for k in ('scan_id', 'snapshot_hash', 'binding', 'expires_at')):
        raise ValueError
    refs = data['refs']
    if (not isinstance(refs, list) or any(type(r) is not str or not 1 <= len(r) <= 255 for r in refs)
            or len(set(refs)) != len(refs)):
        raise ValueError
    prefix = refs[:state['scanned']]
    if len(prefix) != state['scanned'] or digest(prefix) != state['refs_hash']:
        raise ValueError
    return refs


def load_refs(state):
    try:
        path = _path(state)
        if state['scanned'] == 0:
            if state['refs_hash'] != digest([]):
                raise ValueError
            return []
        return _read(path, state)[:state['scanned']]
    except (KeyError, ValueError, OSError, TypeError):
        raise FollowupError('inventory_checkpoint_invalid', 503) from None


def save_refs(state, refs):
    """Commit an exact prefix; parallel/retried pages must agree byte-for-byte."""
    temporary = None
    try:
        path = _path(state)
        if (len(refs) < state['scanned'] or digest(refs[:state['scanned']]) != state['refs_hash']
                or any(type(r) is not str or not 1 <= len(r) <= 255 for r in refs)
                or len(set(refs)) != len(refs)):
            raise ValueError
        fd = os.open(path.with_suffix('.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if path.exists() or path.is_symlink():
                previous = _read(path, state)
                overlap = min(len(previous), len(refs))
                if previous[:overlap] != refs[:overlap]:
                    raise ValueError
                if len(previous) >= len(refs):
                    return
            elif state['scanned']:
                raise ValueError
            data = {k: state[k] for k in ('scan_id', 'snapshot_hash', 'binding', 'expires_at')}
            raw = canonical(data | {'refs': refs})
            if len(raw) > MAX_BYTES:
                raise ValueError
            fd, temporary = tempfile.mkstemp(prefix='.inventory-', dir=path.parent)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            temporary = None
    except (KeyError, ValueError, OSError, TypeError):
        raise FollowupError('inventory_checkpoint_invalid', 503) from None
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)
