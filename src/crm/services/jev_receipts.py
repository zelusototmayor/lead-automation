"""Local shadow receipts, not a CRM database or external exactly-once guarantee.

One filesystem-backed SQLite DB per worker deployment, shared by its processes.
Never delete/expire receipts to retry: pending after its deadline is uncertain.
Recovery/reconciliation may inspect them but this increment exposes no resend.
"""
from __future__ import annotations

from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import time
from uuid import uuid4


class ReceiptStore:
    def __init__(self, path):
        self.path = Path(path)
        if self.path.is_symlink():
            raise ValueError('receipt_symlink')
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        with closing(self._connect()) as db, db:
            db.execute('''CREATE TABLE IF NOT EXISTS jev_receipts (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, expires REAL NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('pending','succeeded','failed','uncertain')),
                result TEXT NOT NULL)''')

    def _connect(self):
        return sqlite3.connect(self.path, timeout=0.05)

    @staticmethod
    def _read(db, receipt_id):
        row = db.execute('SELECT owner, expires, status, result FROM jev_receipts WHERE id=?',
                         (receipt_id,)).fetchone()
        if row is None:
            return None
        result = json.loads(row[3])
        if row[2] == 'pending' and row[1] <= time.time():
            result.update(status='uncertain', error='deadline_or_worker_lost', decisions={})
            db.execute('UPDATE jev_receipts SET status=?,result=? WHERE id=? AND status=?',
                       ('uncertain', json.dumps(result), receipt_id, 'pending'))
        return result

    def get(self, receipt_id):
        with closing(self._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            return self._read(db, receipt_id)

    def claim(self, result, *, deadline_seconds):
        """Commit intent before any transport. Returns (owner_or_None, receipt)."""
        owner = uuid4().hex
        with closing(self._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            existing = self._read(db, result['receipt_id'])
            if existing is not None:
                return None, existing
            pending = dict(result, status='pending', latency_ms=None)
            db.execute('INSERT INTO jev_receipts VALUES (?,?,?,?,?)',
                       (result['receipt_id'], owner, time.time() + deadline_seconds,
                        'pending', json.dumps(pending, allow_nan=False)))
        return owner, pending

    def finish(self, receipt_id, owner, result):
        if result['status'] not in ('succeeded', 'failed', 'uncertain'):
            raise ValueError('terminal_status_required')
        with closing(self._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT owner, expires, status FROM jev_receipts WHERE id=?',
                             (receipt_id,)).fetchone()
            if row is None:
                raise sqlite3.DatabaseError('receipt_missing')
            if row[0] == owner and row[2] == 'pending' and row[1] <= time.time():
                # Keep observed usage/latency even when late hints must be discarded.
                result = dict(result, status='uncertain', decisions={}, error='deadline_or_worker_lost')
            db.execute('''UPDATE jev_receipts SET status=?,result=?
                          WHERE id=? AND owner=? AND status='pending' ''',
                       (result['status'], json.dumps(result, allow_nan=False), receipt_id, owner))
            return self._read(db, receipt_id)
