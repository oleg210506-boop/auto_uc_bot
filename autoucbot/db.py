from __future__ import annotations
import contextlib
import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS secrets(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN('owner','operator','viewer')), active INTEGER NOT NULL DEFAULT 1, totp_secret TEXT, totp_pending TEXT, totp_last INTEGER DEFAULT -1, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS recovery_codes(user_id INTEGER NOT NULL REFERENCES users(id), code_hash TEXT NOT NULL, PRIMARY KEY(user_id,code_hash));
CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES users(id),csrf TEXT NOT NULL,expires REAL NOT NULL,created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS login_attempts(key TEXT PRIMARY KEY,count INTEGER NOT NULL,until REAL NOT NULL);
CREATE TABLE IF NOT EXISTS products(id INTEGER PRIMARY KEY,name TEXT NOT NULL,marker TEXT UNIQUE NOT NULL,fp_lot_id INTEGER NOT NULL,fp_subcategory INTEGER NOT NULL,fp_category INTEGER NOT NULL,sku_id INTEGER NOT NULL,sku_uc INTEGER NOT NULL CHECK(sku_uc>0),multiplier INTEGER NOT NULL CHECK(multiplier>0),region TEXT NOT NULL DEFAULT 'global',uid_field TEXT NOT NULL,extra_delivery TEXT NOT NULL DEFAULT '{}',enabled INTEGER NOT NULL DEFAULT 0,archived INTEGER NOT NULL DEFAULT 0,mode TEXT NOT NULL,sale_price INTEGER NOT NULL DEFAULT 0,min_profit INTEGER NOT NULL DEFAULT 0,max_cost INTEGER NOT NULL DEFAULT 0,auto_price INTEGER NOT NULL DEFAULT 0,markup REAL NOT NULL DEFAULT 15,manage_active INTEGER NOT NULL DEFAULT 1,bot_hidden INTEGER NOT NULL DEFAULT 0,last_price INTEGER,available INTEGER NOT NULL DEFAULT 0,verified INTEGER NOT NULL DEFAULT 0,updated REAL NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS products_lot_mode ON products(fp_lot_id,mode);
CREATE TABLE IF NOT EXISTS catalog(id INTEGER NOT NULL,mode TEXT NOT NULL,name TEXT NOT NULL,price INTEGER NOT NULL,currency TEXT NOT NULL,region TEXT NOT NULL,uc INTEGER NOT NULL,delivery_type TEXT,payload TEXT NOT NULL,updated REAL NOT NULL,PRIMARY KEY(id,mode));
CREATE TABLE IF NOT EXISTS orders(id TEXT PRIMARY KEY,mode TEXT NOT NULL,product_id INTEGER REFERENCES products(id),buyer_id INTEGER NOT NULL,buyer TEXT NOT NULL,chat_id TEXT NOT NULL,quantity INTEGER NOT NULL,uc INTEGER NOT NULL,sku_units INTEGER NOT NULL,snapshot TEXT NOT NULL,uid TEXT,confirm_code TEXT,confirmed INTEGER NOT NULL DEFAULT 0,state TEXT NOT NULL,fp_status TEXT NOT NULL,revenue INTEGER NOT NULL,currency TEXT NOT NULL,fee INTEGER NOT NULL DEFAULT 0,delivered INTEGER NOT NULL DEFAULT 0,note TEXT NOT NULL DEFAULT '',hold_reason TEXT NOT NULL DEFAULT '',created REAL NOT NULL,updated REAL NOT NULL,reminder_count INTEGER NOT NULL DEFAULT 0,last_reminder REAL NOT NULL DEFAULT 0,last_input INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS orders_state ON orders(mode,state,created);
CREATE INDEX IF NOT EXISTS orders_chat ON orders(chat_id,buyer_id,created);
CREATE TABLE IF NOT EXISTS batches(id INTEGER PRIMARY KEY,order_id TEXT NOT NULL REFERENCES orders(id),key TEXT UNIQUE NOT NULL,units INTEGER NOT NULL,uc_per_unit INTEGER NOT NULL,payload TEXT NOT NULL,quote INTEGER NOT NULL,state TEXT NOT NULL,created REAL NOT NULL,first_sent REAL,attempts INTEGER NOT NULL DEFAULT 0,next_check REAL NOT NULL DEFAULT 0,actual_cost INTEGER NOT NULL DEFAULT 0,delivered_units INTEGER NOT NULL DEFAULT 0,net_cost INTEGER NOT NULL DEFAULT 0,last_error TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS batches_due ON batches(state,next_check);
CREATE TABLE IF NOT EXISTS provider_orders(code TEXT PRIMARY KEY,batch_id INTEGER NOT NULL REFERENCES batches(id),state TEXT NOT NULL,total INTEGER NOT NULL DEFAULT 0,delivered_units INTEGER NOT NULL DEFAULT 0,net_cost INTEGER NOT NULL DEFAULT 0,payload TEXT NOT NULL DEFAULT '{}',updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,external_id TEXT,chat_id TEXT NOT NULL,order_id TEXT REFERENCES orders(id),author TEXT NOT NULL,text TEXT NOT NULL,direction TEXT NOT NULL,created REAL NOT NULL,UNIQUE(chat_id,external_id));
CREATE TABLE IF NOT EXISTS seen_messages(chat_id TEXT NOT NULL,message_id TEXT NOT NULL,created REAL NOT NULL,PRIMARY KEY(chat_id,message_id));
CREATE TABLE IF NOT EXISTS chat_controls(chat_id TEXT PRIMARY KEY,manual INTEGER NOT NULL DEFAULT 0,owner_user_id INTEGER REFERENCES users(id),selected_order TEXT,last_seen INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS outbox(id INTEGER PRIMARY KEY,dedupe TEXT UNIQUE NOT NULL,order_id TEXT,chat_id TEXT NOT NULL,text TEXT NOT NULL,kind TEXT NOT NULL DEFAULT 'auto',mode TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,next_attempt REAL NOT NULL DEFAULT 0,created REAL NOT NULL,last_error TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS alerts(id INTEGER PRIMARY KEY,key TEXT UNIQUE NOT NULL,kind TEXT NOT NULL,title TEXT NOT NULL,detail TEXT NOT NULL,order_id TEXT,active INTEGER NOT NULL DEFAULT 1,acked INTEGER NOT NULL DEFAULT 0,last_sent REAL NOT NULL DEFAULT 0,created REAL NOT NULL,updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS alert_deliveries(alert_id INTEGER NOT NULL REFERENCES alerts(id),chat_id TEXT NOT NULL,sent REAL NOT NULL DEFAULT 0,PRIMARY KEY(alert_id,chat_id));
CREATE TABLE IF NOT EXISTS ledger(id INTEGER PRIMARY KEY,mode TEXT NOT NULL,ref TEXT UNIQUE NOT NULL,delta INTEGER NOT NULL,reason TEXT NOT NULL,created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS order_finance(order_id TEXT PRIMARY KEY REFERENCES orders(id),manual_cost INTEGER NOT NULL DEFAULT 0,refund INTEGER NOT NULL DEFAULT 0,note TEXT NOT NULL DEFAULT '',updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS expenses(id INTEGER PRIMARY KEY,amount INTEGER NOT NULL,description TEXT NOT NULL,created REAL NOT NULL,user_id INTEGER REFERENCES users(id));
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY,actor TEXT NOT NULL,action TEXT NOT NULL,entity TEXT NOT NULL,detail TEXT NOT NULL,created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS tasks(id INTEGER PRIMARY KEY,kind TEXT NOT NULL,payload TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'pending',result TEXT NOT NULL DEFAULT '',created REAL NOT NULL,updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS webhooks(event_id TEXT PRIMARY KEY,payload TEXT NOT NULL,received REAL NOT NULL);
CREATE TABLE IF NOT EXISTS runtime(key TEXT PRIMARY KEY,value TEXT NOT NULL,updated REAL NOT NULL);
"""

def dumps(v) -> str:
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

class DB:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.data_dir / "autoucbot.sqlite3"
        with contextlib.closing(self.connect()) as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(SCHEMA)
            c.execute("INSERT OR IGNORE INTO meta VALUES('schema_version','1')")
            c.execute("INSERT OR IGNORE INTO meta VALUES('installation_id',?)", (os.urandom(12).hex(),))
            c.execute("INSERT OR IGNORE INTO settings VALUES('mode','\"demo\"')")
            c.execute("INSERT OR IGNORE INTO settings VALUES('paused','true')")
            c.execute("INSERT OR IGNORE INTO settings VALUES('live_armed','false')")
        from .migrations import migrate
        migrate(self)
        try: os.chmod(self.path, 0o600)
        except OSError: pass

    def connect(self):
        c = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA busy_timeout=15000")
        c.execute("PRAGMA synchronous=FULL")
        return c

    @contextlib.contextmanager
    def tx(self):
        c = self.connect()
        try:
            c.execute("BEGIN IMMEDIATE")
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally: c.close()

    def rows(self, sql, args=()):
        c = self.connect()
        try: return [dict(x) for x in c.execute(sql, args).fetchall()]
        finally: c.close()

    def one(self, sql, args=()):
        rows = self.rows(sql, args)
        return rows[0] if rows else None

    def execute(self, sql, args=()):
        with self.tx() as c:
            cur = c.execute(sql, args)
            return cur.lastrowid

    def setting(self, key, default=None):
        row = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(row["value"]) if row else default

    def set(self, key, value, conn=None):
        args = (key, dumps(value))
        sql = "INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value"
        if conn is not None: conn.execute(sql, args)
        else: self.execute(sql, args)

    def runtime(self, key, value):
        self.execute("INSERT INTO runtime VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated=excluded.updated", (key, dumps(value), time.time()))

    def audit(self, actor, action, entity="", detail="", conn=None):
        args = (str(actor), action, str(entity), str(detail)[:4000], time.time())
        sql = "INSERT INTO audit(actor,action,entity,detail,created) VALUES(?,?,?,?,?)"
        if conn is not None: conn.execute(sql, args)
        else: self.execute(sql, args)

    def backup(self, keep=7) -> Path:
        folder = self.data_dir / "backups"
        folder.mkdir(exist_ok=True)
        name = f"autoucbot-{time.strftime('%Y%m%d-%H%M%S',time.gmtime())}-{os.urandom(2).hex()}.sqlite3"
        target = folder / name
        src, dst = self.connect(), sqlite3.connect(target)
        try: src.backup(dst)
        finally: src.close(); dst.close()
        os.chmod(target, 0o600)
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        target.with_suffix(".sha256").write_text(digest + "  " + name + "\n")
        for old in sorted(folder.glob("*.sqlite3"), key=lambda p:p.stat().st_mtime, reverse=True)[keep:]:
            old.unlink(); old.with_suffix(".sha256").unlink(missing_ok=True)
        return target
