"""Private atomic checkpoint files; standard library only, no CRM runtime imports."""
import os
from pathlib import Path
import tempfile


def private_write(path, content):
    path = Path(path)
    if path.is_symlink():
        raise ValueError('output_symlink')
    fd, name = tempfile.mkstemp(prefix='.jev-note-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)
