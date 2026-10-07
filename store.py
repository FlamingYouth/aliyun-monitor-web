import json
from datetime import datetime
import os
from pathlib import Path
import re
import sqlite3
import time
import threading
from contextlib import contextmanager
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet

DEFAULTS = {"interval": 300, "report_time": "09:00", "timezone": "Asia/Shanghai",
            "dry_run": True, "wecom_enabled": False, "wecom_url": "",
            "mention_all": False, "bark_enabled": False, "bark_url": "",
            "telegram_enabled": False, "telegram_token": "", "telegram_chat_id": "",
            "telegram_proxy": None}
SECRET_SETTINGS = ("wecom_url", "bark_url", "telegram_token")


class Store:
    def __init__(self, directory):
        self.root = Path(directory)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = threading.RLock()
        config_path = Path(os.environ.get("NOTIFICATION_CONFIG", Path(__file__).with_name("notification-config.json")))
        config = json.loads(config_path.read_text()) if config_path.exists() else {}
        self.telegram_proxy = os.environ.get("TELEGRAM_PROXY", config.get("telegram_proxy", ""))
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
            CREATE TABLE IF NOT EXISTS reports (day TEXT PRIMARY KEY, at REAL, content TEXT, delivery TEXT);
            CREATE TABLE IF NOT EXISTS daily_bills (account TEXT, day TEXT, amount REAL, currency TEXT, updated REAL, PRIMARY KEY(account,day));
            CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, csrf TEXT, expires REAL);
            """)
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("UPDATE jobs SET status='interrupted', finished=? WHERE status='running'", (time.time(),))
        os.chmod(self.path, 0o600)
        if self.meta("settings") is None:
            self.set_meta("settings", DEFAULTS)
        self.upgrade_history()

    def upgrade_history(self):
        # Also called after restoring an older backup that has no history tables.
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS reports (day TEXT PRIMARY KEY, at REAL, content TEXT, delivery TEXT);
                CREATE TABLE IF NOT EXISTS daily_bills (account TEXT, day TEXT, amount REAL, currency TEXT, updated REAL, PRIMARY KEY(account,day));
            """)
        previous = self.meta("last_report")
        if previous and previous.get("at"):
            self.save_report(previous["content"], previous.get("delivery", {}), previous["at"])
        self.prune_history()

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
        if values.get("telegram_proxy") is None:
            values["telegram_proxy"] = self.telegram_proxy
        for key in SECRET_SETTINGS:
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
        for key in SECRET_SETTINGS:
            secret = self.settings(True)[key]
            if secret:
                value = value.replace(secret, "[通知地址已隐藏]")
        value = re.sub(r"(?i)(accesskeyid|accesskeysecret|signature|securitytoken|authorization|key|token)([=:]\s*)[^&\s\"'<>]+",
                       r"\1\2[已隐藏]", value)
        value = re.sub(r"bot\d{5,}:[A-Za-z0-9_-]+", "bot[已隐藏]", value)
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

    def account_samples(self, account_id):
        ids = [item["id"] for item in self.rows("instances") if item["account_id"] == account_id]
        if not ids:
            return []
        marks = ",".join("?" for _ in ids)
        # Instances of one account share CDT usage. Keep the latest reading in each
        # minute rather than adding the same account's usage multiple times.
        with self.db() as db:
            rows = db.execute(f"""SELECT at,traffic FROM (
                SELECT at,traffic,ROW_NUMBER() OVER (PARTITION BY CAST(at/60 AS INTEGER) ORDER BY at DESC,id DESC) AS position
                FROM samples WHERE instance IN ({marks})
            ) WHERE position=1 ORDER BY at DESC LIMIT 144""", ids).fetchall()
        return [dict(row) for row in reversed(rows)]

    def account_daily_samples(self, account_id):
        timezone = ZoneInfo(self.settings()["timezone"])
        now = datetime.fromtimestamp(time.time(), timezone)
        ids = [item["id"] for item in self.rows("instances") if item["account_id"] == account_id]
        daily = {}
        if ids:
            marks = ",".join("?" for _ in ids)
            start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()
            with self.db() as db:
                rows = db.execute(f"SELECT at,traffic FROM samples WHERE instance IN ({marks}) AND at>=? AND at<=? ORDER BY at,id",
                                  [*ids, start, time.time()]).fetchall()
            for row in rows:
                day = datetime.fromtimestamp(row["at"], timezone).strftime("%Y-%m-%d")
                daily[day] = dict(row)
        # Explicitly supplied historical daily values override older raw samples.
        # A new sample today wins as soon as it is newer than the manual entry.
        for day, row in self.meta("traffic_history", {}).get(account_id, {}).items():
            if day[:7] == now.strftime("%Y-%m") and day <= now.strftime("%Y-%m-%d"):
                if day not in daily or row["at"] >= daily[day]["at"]:
                    daily[day] = row
        return [{"at": datetime.fromisoformat(day).replace(tzinfo=timezone).timestamp(),
                 "traffic": row["traffic"]} for day, row in sorted(daily.items())]

    def month(self, at=None):
        return datetime.fromtimestamp(time.time() if at is None else at,
                                      ZoneInfo(self.settings()["timezone"])).strftime("%Y-%m")

    def prune_history(self, at=None):
        month = self.month(at)
        with self.db() as db:
            db.execute("DELETE FROM jobs WHERE status!='running' AND id NOT IN (SELECT id FROM jobs ORDER BY at DESC,rowid DESC LIMIT 3)")
            db.execute("DELETE FROM reports WHERE substr(day,1,7)!=?", (month,))
            db.execute("DELETE FROM daily_bills WHERE substr(day,1,7)!=?", (month,))
            row = db.execute("SELECT at,content,delivery FROM reports ORDER BY day DESC LIMIT 1").fetchone()
            if row:
                value = {"at": row["at"], "content": row["content"], "delivery": json.loads(row["delivery"])}
                db.execute("INSERT OR REPLACE INTO meta VALUES ('last_report',?)", (json.dumps(value, ensure_ascii=False),))
            else:
                db.execute("DELETE FROM meta WHERE key='last_report'")
            row = db.execute("SELECT value FROM meta WHERE key='traffic_history'").fetchone()
            if row:
                history = {account: {day: value for day, value in days.items() if day[:7] == month}
                           for account, days in json.loads(row["value"]).items()}
                db.execute("INSERT OR REPLACE INTO meta VALUES ('traffic_history',?)", (json.dumps(history),))

    def save_report(self, content, delivery, at=None):
        at = time.time() if at is None else at
        day = datetime.fromtimestamp(at, ZoneInfo(self.settings()["timezone"])).strftime("%Y-%m-%d")
        with self.db() as db:
            db.execute("INSERT OR REPLACE INTO reports VALUES (?,?,?,?)",
                       (day, at, content, json.dumps(delivery, ensure_ascii=False)))
        self.prune_history()

    def reports(self):
        with self.db() as db:
            return [{"day": row["day"], "at": row["at"], "delivery": json.loads(row["delivery"])}
                    for row in db.execute("SELECT day,at,delivery FROM reports ORDER BY day DESC")]

    def report(self, day):
        with self.db() as db:
            row = db.execute("SELECT * FROM reports WHERE day=?", (day,)).fetchone()
        return {**dict(row), "delivery": json.loads(row["delivery"])} if row else None

    def daily_bills(self, account_id, month):
        with self.db() as db:
            return [dict(row) for row in db.execute(
                "SELECT day,amount,currency,updated FROM daily_bills WHERE account=? AND substr(day,1,7)=? ORDER BY day",
                (account_id, month))]

    def save_daily_bill(self, account_id, day, bill):
        with self.db() as db:
            db.execute("INSERT OR REPLACE INTO daily_bills VALUES (?,?,?,?,?)",
                       (account_id, day, bill["amount"], bill["currency"], time.time()))
