"""七張 SQLite 表；網路操作不持有交易，複合寫入以短交易提交。"""

from contextlib import asynccontextmanager
import asyncio
import json
from pathlib import Path

import aiosqlite


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
 id TEXT PRIMARY KEY, city TEXT, district TEXT, dataset TEXT, location_code TEXT,
 wake_time TEXT NOT NULL DEFAULT '07:00', timezone TEXT NOT NULL DEFAULT 'Asia/Taipei',
 morning_enabled INTEGER NOT NULL DEFAULT 1, alerts_enabled INTEGER NOT NULL DEFAULT 1,
 night_forecast_enabled INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1,
 setup_step TEXT NOT NULL DEFAULT 'region', setup_data TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS messages (
 id INTEGER PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), role TEXT NOT NULL,
 content TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'chat', turn INTEGER,
 created_at REAL NOT NULL, external_event_id TEXT UNIQUE, event_timestamp REAL
);
CREATE INDEX IF NOT EXISTS messages_user ON messages(user_id,id);
CREATE TABLE IF NOT EXISTS conversation_state (
 user_id TEXT PRIMARY KEY REFERENCES users(id), summary TEXT NOT NULL DEFAULT '{}',
 summary_through INTEGER NOT NULL DEFAULT 0, memory_processed_until INTEGER NOT NULL DEFAULT 0,
 forgotten_through INTEGER NOT NULL DEFAULT 0,
 choices TEXT, choice_set_id TEXT, choices_expires REAL, version INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS memories (
 id INTEGER PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), scope TEXT NOT NULL,
 category TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, source_message_ids TEXT NOT NULL,
 expires_at REAL, version INTEGER NOT NULL DEFAULT 1, source_until INTEGER NOT NULL,
 forgotten INTEGER NOT NULL DEFAULT 0, UNIQUE(user_id,scope,key)
);
CREATE TABLE IF NOT EXISTS weather_snapshots (
 id INTEGER PRIMARY KEY, location_code TEXT NOT NULL, source_issued_at TEXT,
 forecast_hash TEXT NOT NULL, fetched_at REAL NOT NULL, data TEXT NOT NULL,
 UNIQUE(location_code,forecast_hash)
);
CREATE TABLE IF NOT EXISTS jobs (
 id INTEGER PRIMARY KEY, kind TEXT NOT NULL, user_id TEXT, business_key TEXT NOT NULL UNIQUE,
 payload TEXT NOT NULL, run_at REAL NOT NULL, deadline REAL, status TEXT NOT NULL DEFAULT 'pending',
 attempts INTEGER NOT NULL DEFAULT 0, error TEXT, created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_due ON jobs(status,kind,run_at);
CREATE TABLE IF NOT EXISTS outbox (
 id INTEGER PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), business_key TEXT NOT NULL UNIQUE,
 kind TEXT NOT NULL, method TEXT NOT NULL, payload TEXT NOT NULL, retry_key TEXT,
 weather TEXT NOT NULL DEFAULT '[]', memory_ids TEXT NOT NULL DEFAULT '[]',
 send_at REAL NOT NULL, expires_at REAL NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
 attempts INTEGER NOT NULL DEFAULT 0, sent_at REAL, error TEXT
);
"""


class Database:
    def __init__(self, path):
        self.path = path
        self.lock = asyncio.Lock()

    async def open(self):
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = await aiosqlite.connect(self.path, isolation_level=None)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.execute("PRAGMA foreign_keys=ON")
        await self.conn.execute("PRAGMA busy_timeout=5000")
        await self.conn.executescript(SCHEMA)
        async with self.conn.execute("PRAGMA table_info(users)") as cursor:
            columns = {row["name"] for row in await cursor.fetchall()}
        if "setup_data" not in columns:
            await self.conn.execute("ALTER TABLE users ADD COLUMN setup_data TEXT NOT NULL DEFAULT '{}'")
        return self

    async def close(self):
        await self.conn.close()

    @asynccontextmanager
    async def transaction(self):
        async with self.lock:
            await self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
                await self.conn.commit()
            except BaseException:
                await self.conn.rollback()
                raise

    async def execute(self, sql, params=()):
        async with self.lock:
            cursor = await self.conn.execute(sql, params)
            return cursor.lastrowid

    async def all(self, sql, params=()):
        async with self.lock:
            async with self.conn.execute(sql, params) as cursor:
                return [dict(row) for row in await cursor.fetchall()]

    async def one(self, sql, params=()):
        rows = await self.all(sql, params)
        return rows[0] if rows else None

    async def ensure_user(self, user_id):
        async with self.transaction() as conn:
            await conn.execute("INSERT OR IGNORE INTO users(id) VALUES (?)", (user_id,))
            await conn.execute("INSERT OR IGNORE INTO conversation_state(user_id) VALUES (?)", (user_id,))

    async def job(self, kind, user_id, key, payload, now, *, run_at=None, deadline=None):
        async with self.transaction() as conn:
            await conn.execute(
                "INSERT OR IGNORE INTO jobs(kind,user_id,business_key,payload,run_at,deadline,created_at) VALUES (?,?,?,?,?,?,?)",
                (kind, user_id, key, dumps(payload), now if run_at is None else run_at, deadline, now),
            )
            text = payload.get("message", {}).get("text") if kind == "chat" else None
            if text:
                await conn.execute("INSERT OR IGNORE INTO messages(user_id,role,content,kind,created_at,external_event_id,event_timestamp) VALUES (?,'user',?,'received',?,?,?)",
                    (user_id, text, now, payload.get("webhookEventId"), payload.get("timestamp", 0) / 1000))

    async def recover(self):
        # Reply 無法安全重試；程序可能已送出但未記錄結果。
        rows = await self.all("SELECT * FROM jobs WHERE kind='chat' AND status='running'")
        for row in rows:
            event = json.loads(row["payload"])
            text = event.get("message", {}).get("text")
            if text:
                await self.execute("INSERT OR IGNORE INTO messages(user_id,role,content,kind,created_at,external_event_id,event_timestamp) VALUES (?,'user',?,'received',?,?,?)",
                    (row["user_id"], text, row["created_at"], event.get("webhookEventId"), event.get("timestamp", 0) / 1000))
        await self.execute("UPDATE jobs SET status='uncertain',payload='{}',error='restart_after_reply_claim' WHERE kind='chat' AND status='running'")
        await self.execute("UPDATE jobs SET status='pending' WHERE kind!='chat' AND status='running'")
        await self.execute("UPDATE outbox SET status='pending' WHERE method='push' AND status='sending'")
        await self.execute("UPDATE outbox SET status='uncertain' WHERE method='reply' AND status='sending'")
