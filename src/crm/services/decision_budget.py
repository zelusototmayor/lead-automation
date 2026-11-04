"""Single-lane durable conservative reservations; uncertainty is never refunded."""
import sqlite3
from pathlib import Path


class BudgetStop(RuntimeError):
    pass


class Ledger:
    def __init__(self, path, *, cap_microeur):
        self.path = Path(path)
        if type(cap_microeur) is not int or not 0 < cap_microeur <= 20_000_000:
            raise ValueError('Invalid aggregate cap')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.cap = cap_microeur
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS reservations (id TEXT PRIMARY KEY, microeur INTEGER NOT NULL CHECK(microeur>0))')
            db.execute('CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1), cap INTEGER NOT NULL)')
            db.execute('INSERT OR IGNORE INTO settings VALUES(1, ?)', (self.cap,))
            if db.execute('SELECT cap FROM settings WHERE id=1').fetchone()[0] != self.cap:
                raise BudgetStop('Ledger cap cannot change')

    def reserve(self, identity, amount_microeur):
        if not isinstance(identity, str) or not identity or type(amount_microeur) is not int or amount_microeur <= 0:
            raise ValueError('Invalid reservation')
        with sqlite3.connect(self.path, timeout=10) as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT microeur FROM reservations WHERE id=?',(identity,)).fetchone()
            if existing:
                if existing[0] != amount_microeur:
                    raise BudgetStop('Reservation identity reused with different amount')
                return False
            used = db.execute('SELECT COALESCE(SUM(microeur),0) FROM reservations').fetchone()[0]
            if used + amount_microeur > self.cap:
                raise BudgetStop('Aggregate budget exhausted')
            db.execute('INSERT INTO reservations VALUES(?,?)',(identity, amount_microeur))
            return True

    def total(self):
        with sqlite3.connect(self.path) as db:
            return db.execute('SELECT COALESCE(SUM(microeur),0) FROM reservations').fetchone()[0]
