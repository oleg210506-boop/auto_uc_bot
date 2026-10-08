"""Additive, transactional schema upgrades. Financial history is never discarded."""
from __future__ import annotations
import fcntl
import json
import time

SCHEMA_V2 = [
    "ALTER TABLE products ADD COLUMN service TEXT NOT NULL DEFAULT 'uc'",
    "ALTER TABLE products ADD COLUMN supplier TEXT NOT NULL DEFAULT 'gamecore'",
    "ALTER TABLE orders ADD COLUMN service TEXT NOT NULL DEFAULT 'uc'",
    "ALTER TABLE orders ADD COLUMN supplier TEXT NOT NULL DEFAULT 'gamecore'",
    """CREATE TABLE fazer_skus (
        id INTEGER PRIMARY KEY AUTOINCREMENT, mode TEXT NOT NULL, service TEXT NOT NULL,
        category_id TEXT NOT NULL, offer_id TEXT NOT NULL, name TEXT NOT NULL,
        units INTEGER NOT NULL DEFAULT 0, units_confirmed INTEGER NOT NULL DEFAULT 0,
        price_usd TEXT NOT NULL, fields TEXT NOT NULL, raw TEXT NOT NULL, updated REAL NOT NULL,
        UNIQUE(mode,service,category_id,offer_id))""",
    """CREATE TABLE fazer_parts (
        id INTEGER PRIMARY KEY, batch_id INTEGER NOT NULL REFERENCES batches(id),
        ordinal INTEGER NOT NULL, mode TEXT NOT NULL, service TEXT NOT NULL, units INTEGER NOT NULL CHECK(units>0),
        idem_key TEXT NOT NULL UNIQUE, body TEXT NOT NULL, body_hash TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'prepared', provider_id TEXT,
        quoted_micros INTEGER NOT NULL, reserved_micros INTEGER NOT NULL,
        charged_micros INTEGER, cost_source TEXT NOT NULL DEFAULT 'quote',
        fx TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, first_sent REAL,
        next_check REAL NOT NULL DEFAULT 0, response TEXT NOT NULL DEFAULT '{}',
        error TEXT NOT NULL DEFAULT '', created REAL NOT NULL, updated REAL NOT NULL,
        UNIQUE(batch_id,ordinal), UNIQUE(mode,provider_id))""",
    "CREATE INDEX fazer_parts_due ON fazer_parts(state,next_check)",
    """CREATE TABLE fazer_wallet (mode TEXT PRIMARY KEY, micros INTEGER NOT NULL,
        identity TEXT NOT NULL DEFAULT '', updated REAL NOT NULL)""",
    "CREATE INDEX orders_service ON orders(mode,service,created)",
]


def migrate(db):
    version = db.one("SELECT value FROM meta WHERE key='schema_version'")["value"]
    if version == '2':
        return
    if version != '1':
        raise RuntimeError('Неизвестная версия базы. Обновление остановлено, данные не изменены.')
    with (db.data_dir / 'instance.lock').open('a+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Перед обновлением остановите старый процесс autoUCbot.') from None
        # An old installation with real data gets a complete, consistent backup before ALTER.
        existing = db.one('SELECT COUNT(*) n FROM users')["n"] > 0
        if existing:
            db.backup(20)
        with db.tx() as c:
            current = c.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
            if current != '1':
                return
            for statement in SCHEMA_V2:
                c.execute(statement)
            c.execute("UPDATE meta SET value='2' WHERE key='schema_version'")
            if existing:
                # Credentials and old supplier operations are retained for read-only reconciliation.
                c.execute("UPDATE products SET enabled=0,verified=0 WHERE mode='live'")
                c.execute("UPDATE orders SET state='manual',confirmed=0,hold_reason='Переход на FazerCards: старый заказ требует сверки' WHERE mode='live' AND state IN ('ready','awaiting_uid','awaiting_confirmation','waiting_balance') AND NOT EXISTS (SELECT 1 FROM batches b WHERE b.order_id=orders.id)")
                for k, value in {'paused': True, 'live_armed': False, 'mode': 'observe',
                                 'pause_reason': 'Обновление поставщика: настройте FazerCards и проверьте товары',
                                 'fees_confirmed': False, 'provider_terms_confirmed': False,
                                 'dynamic_price_accepted': False}.items():
                    db.set(k, value, c)
                # Stale GameCore messages must not be sent for the new supplier.
                c.execute("UPDATE outbox SET state='cancelled' WHERE mode='live' AND state='pending'")
                db.audit('migration', 'schema.v2', detail='История сохранена; новые закупки выключены', conn=c)
        fcntl.flock(lock, fcntl.LOCK_UN)
