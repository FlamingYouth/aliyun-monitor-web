import json
import os
from pathlib import Path
import re
import sqlite3
import time
import threading
from contextlib import contextmanager

from cryptography.fernet import Fernet

DEFAULTS = {"interval": 300, "report_time": "09:00", "timezone": "Asia/Shanghai",
            "dry_run": True, "wecom_enabled": False, "wecom_url": "",
            "mention_all": False, "bark_enabled": False, "bark_url": ""}


class Store:
    def __init__(self, directory):
        self.root = Path(directory)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = threading.RLock()
        key = self.root / "secret.key"
        if not key.exists():
            fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(Fernet.generate_key())
        self.cipher = Fernet(key.read_bytes())
        self.path = self.root / "monitor.db"
        with self.db() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS accounts (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS instances (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, at REAL, level TEXT, kind TEXT, instance TEXT, message TEXT);
            CREATE TABLE IF NOT EXISTS samples (id INTEGER PRIMARY KEY, at REAL, instance TEXT, traffic REAL, status TEXT);
            CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, kind TEXT, status TEXT, at REAL, finished REAL, result TEXT);
            CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, csrf TEXT, expires REAL);
            """)
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("UPDATE jobs SET status='interrupted', finished=? WHERE status='running'", (time.time(),))
        os.chmod(self.path, 0o600)
        if self.meta("settings") is None:
            self.set_meta("settings", DEFAULTS)

    @contextmanager
    def db(self):
        with self.lock:
            db = sqlite3.connect(self.path, timeout=10)
            db.row_factory = sqlite3.Row
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise
            finally:
                db.close()

    def meta(self, key, default=None):
        with self.db() as db:
            row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_meta(self, key, value):
        with self.db() as db:
            db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, json.dumps(value, ensure_ascii=False)))

    def seal(self, value):
        return self.cipher.encrypt(str(value).encode()).decode() if value else ""

    def unseal(self, value):
        return self.cipher.decrypt(value.encode()).decode() if value else ""

    def rows(self, table):
        assert table in ("accounts", "instances")
        with self.db() as db:
            rows = db.execute(f"SELECT payload FROM {table} ORDER BY rowid").fetchall()
        return [json.loads(row[0]) for row in rows]

    def get(self, table, item_id):
        return next((row for row in self.rows(table) if row["id"] == item_id), None)

    def put(self, table, item):
        assert table in ("accounts", "instances")
        with self.db() as db:
            db.execute(f"INSERT OR REPLACE INTO {table} VALUES (?,?)", (item["id"], json.dumps(item, ensure_ascii=False)))

    def delete(self, table, item_id):
        assert table in ("accounts", "instances")
        with self.db() as db:
            db.execute(f"DELETE FROM {table} WHERE id=?", (item_id,))

    def write_config(self, accounts, instances, metadata):
        """Commit a complete setup/import together, including its secrets."""
        with self.db() as db:
            for table, items in (("accounts", accounts), ("instances", instances)):
                for item in items:
                    db.execute(f"INSERT OR REPLACE INTO {table} VALUES (?,?)", (item["id"], json.dumps(item, ensure_ascii=False)))
            for key, value in metadata.items():
                db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, json.dumps(value, ensure_ascii=False)))

    def settings(self, secret=False):
        values = {**DEFAULTS, **self.meta("settings", {})}
        for key in ("wecom_url", "bark_url"):
            value = self.unseal(values[key])
            values[key] = value if secret else ""
            if not secret:
                values[key + "_configured"] = bool(value)
        return values

    def account(self, account_id):
        account = self.get("accounts", account_id)
        if account:
            account = {**account, "ak": self.unseal(account["ak"]), "sk": self.unseal(account["sk"])}
        return account

    def redact(self, value, limit=1500):
        value = str(value)
        for account in self.rows("accounts"):
            for key in ("ak", "sk"):
                secret = self.unseal(account[key])
                if secret:
                    value = value.replace(secret, "[已隐藏]")
        for key in ("wecom_url", "bark_url"):
            secret = self.settings(True)[key]
            if secret:
                value = value.replace(secret, "[通知地址已隐藏]")
        value = re.sub(r"(?i)(accesskeyid|accesskeysecret|signature|securitytoken|authorization|key|token)([=:]\s*)[^&\s\"'<>]+",
                       r"\1\2[已隐藏]", value)
        return value if limit is None else value[:limit]

    def event(self, level, kind, message, instance=""):
        with self.db() as db:
            db.execute("INSERT INTO events(at,level,kind,instance,message) VALUES (?,?,?,?,?)",
                       (time.time(), level, kind, instance, self.redact(message)))
            db.execute("DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT 2000)")

    def events(self, limit=100):
        with self.db() as db:
            return [dict(row) for row in db.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))]

    def sample(self, item_id, traffic, status):
        with self.db() as db:
            db.execute("INSERT INTO samples(at,instance,traffic,status) VALUES (?,?,?,?)", (time.time(), item_id, traffic, status))
            db.execute("DELETE FROM samples WHERE at<?", (time.time() - 31 * 86400,))

    def samples(self, item_id):
        with self.db() as db:
            rows = db.execute("SELECT at,traffic,status FROM samples WHERE instance=? ORDER BY id DESC LIMIT 144", (item_id,)).fetchall()
        return [dict(row) for row in reversed(rows)]
