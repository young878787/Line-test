"""單程序服務：聊天即時喚醒，背景排程／記憶獨立執行。"""

import asyncio
import calendar
from datetime import datetime, timedelta
import json
import logging
import uuid

import httpx

from weather_bot.ai import AIClient
from weather_bot.chat import ChatService, fallback, weather_facts
from weather_bot.config import Clock, TZ
from weather_bot.controller import process_chat, receive
from weather_bot.db import Database, dumps
from weather_bot.line_io import LineSender, message_payload
from weather_bot.memory import MemoryService
from weather_bot.weather import WeatherService, is_night, merge_rain, parse_time

logger = logging.getLogger(__name__)


class Bot:
    def __init__(self, settings, *, clock=None, http=None, ai=None, sender=None):
        self.settings, self.clock = settings, clock or Clock()
        self.http = http or httpx.AsyncClient()
        self.owns_http = http is None
        self.db = Database(settings.database)
        self.ai = ai or AIClient(settings, self.http)
        self.sender = sender or LineSender(settings.line_token, self.http)
        self.weather = WeatherService(self.db, self.http, settings.cwa_key, self.clock)
        self.memory = MemoryService(self.db, self.ai, self.clock, settings)
        self.chat = ChatService(self.db, self.ai, self.memory, self.clock, settings)
        self.user_locks = {}
        self.wakeup = asyncio.Event()
        self.tasks, self.chat_tasks = [], {}

    async def start(self, *, workers=True):
        await self.db.open()
        await self.db.recover()
        if workers:
            self.tasks = [asyncio.create_task(self.chat_loop()), asyncio.create_task(self.background_loop())]
        return self

    async def stop(self):
        tasks = self.tasks + list(self.chat_tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.db.close()
        if self.owns_http:
            await self.http.aclose()

    async def receive(self, event, received_at=None):
        await receive(self, event, self.clock.now().timestamp() if received_at is None else received_at)

    async def finish_job(self, job_id, status, error=None):
        async with self.db.transaction() as conn:
            async with conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)) as cursor:
                job = await cursor.fetchone()
            if job and job["kind"] == "chat":
                event = json.loads(job["payload"])
                text = event.get("message", {}).get("text")
                if text:
                    # 失敗或逾期的輸入也保留原文，但不算完整問答或交給記憶整理。
                    await conn.execute("INSERT OR IGNORE INTO messages(user_id,role,content,kind,created_at,external_event_id,event_timestamp) VALUES (?,'user',?,'received',?,?,?)",
                        (job["user_id"], text, job["created_at"], event.get("webhookEventId"), event.get("timestamp", 0) / 1000))
            await conn.execute("UPDATE jobs SET status=?,error=?,payload=CASE WHEN kind='chat' THEN '{}' ELSE payload END WHERE id=?", (status, error, job_id))

    async def dispatch_chats(self):
        rows = await self.db.all("SELECT * FROM jobs WHERE kind='chat' AND status='pending' ORDER BY id")
        for row in rows:
            await self.db.execute("UPDATE jobs SET status='running',attempts=attempts+1 WHERE id=?", (row["id"],))
            self.chat_tasks[row["id"]] = asyncio.create_task(self.run_chat(row))

    async def run_chat(self, row):
        try:
            await process_chat(self, row)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self.finish_job(row["id"], "failed", type(exc).__name__)
        finally:
            self.chat_tasks.pop(row["id"], None)

    async def drain_chats(self):
        await self.dispatch_chats()
        await asyncio.gather(*list(self.chat_tasks.values()))

    async def chat_loop(self):
        while True:
            self.wakeup.clear()
            await self.dispatch_chats()
            try:
                await asyncio.wait_for(self.wakeup.wait(), 0.5)
            except TimeoutError:
                pass

    async def queue_memory(self, uid, urgent=False):
        state = await self.db.one("SELECT * FROM conversation_state WHERE user_id=?", (uid,))
        turns = await self.db.all("SELECT turn,MAX(id) AS id FROM messages WHERE user_id=? AND kind='chat' AND turn IS NOT NULL AND id>? GROUP BY turn HAVING COUNT(*)=2 ORDER BY turn", (uid, state["summary_through"]))
        now = self.clock.now().timestamp()
        if not turns:
            return
        length = await self.db.one("SELECT SUM(LENGTH(content)) AS size FROM messages WHERE user_id=? AND kind='chat' AND id>?", (uid, state["summary_through"]))
        if len(turns) >= 20 or len(turns) >= 6 and (length["size"] or 0) > self.settings.input_budget:
            await self.db.job("compact", uid, f"compact:{uid}:{state['summary_through']}", {}, now)
        pending = await self.db.one("SELECT COUNT(*) AS n,MAX(id) AS last FROM messages WHERE user_id=? AND role='assistant' AND kind='chat' AND id>?", (uid, state["memory_processed_until"]))
        if pending["n"] >= 5 or urgent:
            # 每個使用者同時只保留一個待處理批次；執行時讀取最新完整輪次。
            active = await self.db.one("SELECT id FROM jobs WHERE user_id=? AND kind='memory' AND status='pending'", (uid,))
            if not active:
                await self.db.job("memory", uid, f"memory:{uid}:{pending['last']}", {}, now)

    async def tick(self):
        now = self.clock.now()
        epoch = now.timestamp()
        users = await self.db.all("SELECT * FROM users WHERE active=1 AND setup_step='done'")
        # 每小時 :40；重啟時讀取最近應執行的小時，business_key 防重。
        slot = now.replace(minute=40, second=0, microsecond=0)
        if slot > now:
            slot -= timedelta(hours=1)
        for dataset, city in sorted({(u["dataset"], u["city"]) for u in users}):
            await self.db.job("weather", None, f"weather:{dataset}:{city}:{slot.isoformat()}", {"dataset": dataset, "city": city}, epoch)
        for user in users:
            uid = user["id"]
            wake = datetime.combine(now.date(), datetime.strptime(user["wake_time"], "%H:%M").time(), TZ)
            # 00:05 起床的準備發生於前一天。
            for target in (wake, wake + timedelta(days=1)):
                if user["morning_enabled"] and target - timedelta(minutes=10) <= now <= target + timedelta(minutes=20):
                    key = f"morning:{uid}:{target.date()}"
                    payload = {"target": target.isoformat(), "location_code": user["location_code"]}
                    await self.db.job("morning", uid, key, payload, epoch, deadline=(target + timedelta(minutes=20)).timestamp())
                    await self.db.execute("UPDATE jobs SET payload=?,status='pending',attempts=0,run_at=?,deadline=? WHERE business_key=? AND status!='running' AND payload!=?",
                        (dumps(payload), epoch, (target + timedelta(minutes=20)).timestamp(), key, dumps(payload)))
            await self.queue_memory(uid)
            latest = await self.db.one("SELECT MAX(created_at) AS time FROM messages WHERE user_id=? AND role='assistant' AND kind='chat'", (uid,))
            if latest["time"] and epoch - latest["time"] >= 120:
                await self.queue_memory(uid, urgent=True)

    async def background_loop(self):
        while True:
            try:
                await self.tick()
                # 先送已準備內容，再整理記憶，避免慢記憶阻塞到期的 outbox。
                await self.send_due()
                await self.run_background()
                await self.send_due()
            except Exception as exc:
                logger.error("背景工作失敗：%s", type(exc).__name__)
            await asyncio.sleep(1)

    async def run_background(self):
        rows = await self.db.all("SELECT * FROM jobs WHERE kind!='chat' AND status='pending' AND run_at<=? ORDER BY CASE kind WHEN 'morning' THEN 0 WHEN 'weather' THEN 1 WHEN 'compact' THEN 2 ELSE 3 END,id", (self.clock.now().timestamp(),))
        for row in rows:
            now = self.clock.now().timestamp()
            if row["deadline"] and now > row["deadline"]:
                await self.finish_job(row["id"], "expired")
                continue
            await self.db.execute("UPDATE jobs SET status='running',attempts=attempts+1 WHERE id=?", (row["id"],))
            try:
                payload = json.loads(row["payload"])
                if row["kind"] in {"memory", "compact"}:
                    await self.memory.organize(row["user_id"], compact=row["kind"] == "compact")
                elif row["kind"] == "morning":
                    await self.prepare_morning(row["user_id"], parse_time(payload["target"]))
                elif row["kind"] == "weather":
                    await self.poll_weather(**payload)
                await self.finish_job(row["id"], "done")
            except Exception as exc:
                attempts = row["attempts"] + 1
                await self.db.execute("UPDATE jobs SET status=?,run_at=?,error=? WHERE id=?", ("failed" if attempts >= 3 else "pending", now + 30 * attempts, type(exc).__name__, row["id"]))

    async def push_budget(self, *, extra=False):
        now = self.clock.now()
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()
        count = await self.db.one("SELECT COUNT(*) AS n FROM outbox WHERE method='push' AND send_at>=? AND status IN ('pending','sending','sent')", (start,))
        used = count["n"]
        # LINE 官方帳號可能還有其他發訊來源；額外提醒前以官方用量再確認。
        if extra:
            try:
                limit, official_used = await self.sender.quota()
                used = max(used, official_used + (await self.db.one("SELECT COUNT(*) AS n FROM outbox WHERE method='push' AND status IN ('pending','sending')"))["n"])
                available = min(self.settings.monthly_limit, limit) if limit is not None else self.settings.monthly_limit
            except Exception:
                return False
        else:
            available = self.settings.monthly_limit
        reserve = 0
        if extra:
            users = await self.db.all("SELECT * FROM users WHERE active=1 AND setup_step='done' AND morning_enabled=1")
            remaining_days = calendar.monthrange(now.year, now.month)[1] - now.day
            reserve = len(users) * remaining_days
            for user in users:
                if now.strftime("%H:%M") <= user["wake_time"]:
                    existing = await self.db.one("SELECT id FROM outbox WHERE business_key=? AND status IN ('pending','sending','sent')", (f"morning:{user['id']}:{now.date()}",))
                    reserve += int(existing is None)
        return used + reserve < available

    async def prepare_morning(self, uid, target):
        user = await self.db.one("SELECT * FROM users WHERE id=?", (uid,))
        if not user["active"] or not user["morning_enabled"] or target.strftime("%H:%M") != user["wake_time"]:
            return
        key = f"morning:{uid}:{target.date()}"
        previous = await self.db.one("SELECT * FROM outbox WHERE business_key=?", (key,))
        if previous:
            metadata = json.loads(previous["weather"])
            if previous["attempts"] or previous["status"] == "sent":
                return
            if previous["status"] == "pending" and metadata["wake_time"] == user["wake_time"] and metadata["location_code"] == user["location_code"]:
                return
        elif not await self.push_budget():
            return
        try:
            weather = await self.weather.get_weather(user, (target, target + timedelta(hours=18)))
        except Exception:
            weather = None
        await self.prepare_push(user, key, "morning_brief", weather, target.timestamp(), (target + timedelta(minutes=20)).timestamp())

    async def prepare_push(self, user, key, mode, weather, send_at, expires):
        at = datetime.fromtimestamp(send_at, TZ) if mode == "morning_brief" else self.clock.now()
        facts = weather_facts(user, weather, mode, at)
        try:
            async with asyncio.timeout(self.settings.chat_timeout):
                reply, context = await self.chat.generate(user, "", mode=mode, weather=weather, fresh=True, at=at)
        except Exception:
            context = await self.chat.context(user["id"], fresh=True)
            if mode == "morning_brief" and len(facts) == 4:
                address = context["persona"].get("address_user_as", "")
                facts += [f"{address}，今天也記得照顧自己" if address else "今天也記得照顧自己", "哼，準備好了就出發吧"]
            reply = fallback(facts)
        choice_id = uuid.uuid4().hex
        payload = message_payload(reply, choice_id)
        periods = weather.get("periods", []) if weather else []
        # 只標記正文實際告知的降雨時段，不能把整份快取視為已告知。
        mentioned = [p for p in periods if (p["rain_state"] == "rain" or (p["pop_3h"] or 0) >= 60)
                     and f"{parse_time(p['valid_from']):%m-%d %H:%M}至{parse_time(p['valid_to']):%H:%M}" in "".join(reply.sentences)]
        if mode == "weather_alert":
            mentioned = periods
        metadata = {"location_code": user["location_code"], "wake_time": user["wake_time"], "periods": merge_rain(mentioned),
                    "choices": [c.model_dump() for c in reply.choices], "choice_set_id": choice_id}
        await self.db.execute("""INSERT INTO outbox(user_id,business_key,kind,method,payload,retry_key,weather,memory_ids,send_at,expires_at) VALUES (?,?,?,'push',?,?,?,?,?,?)
            ON CONFLICT(business_key) DO UPDATE SET payload=excluded.payload,retry_key=excluded.retry_key,weather=excluded.weather,
            memory_ids=excluded.memory_ids,send_at=excluded.send_at,expires_at=excluded.expires_at,status='pending'
            WHERE outbox.attempts=0 AND outbox.status!='sent'""",
            (user["id"], key, mode, dumps(payload), str(uuid.uuid4()), dumps(metadata), dumps([m["id"] for m in context.get("memories", [])]), send_at, expires))

    async def alert_allowed(self, user):
        now = self.clock.now()
        if not user["active"] or not user["alerts_enabled"]:
            return False
        if is_night(now):
            if not user["night_forecast_enabled"]:
                return False
        elif now.strftime("%H:%M") < user["wake_time"]:
            return False
        start = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        daily = await self.db.one("SELECT id FROM outbox WHERE user_id=? AND kind='weather_alert' AND send_at>=? AND status IN ('pending','sending','sent')", (user["id"], start))
        return daily is None and await self.push_budget(extra=True)

    async def poll_weather(self, dataset, city):
        weather, candidates = await self.weather.refresh(dataset, city)
        if not candidates:
            return
        users = await self.db.all("SELECT * FROM users WHERE dataset=? AND city=? AND setup_step='done'", (dataset, city))
        for user in users:
            if not await self.alert_allowed(user):
                continue
            previous = await self.db.all("SELECT weather FROM outbox WHERE user_id=? AND status='sent' AND method='push' AND expires_at>?", (user["id"], self.clock.now().timestamp() - 86400))
            informed = [p for row in previous if json.loads(row["weather"]).get("location_code") == user["location_code"] for p in json.loads(row["weather"]).get("periods", [])]
            relevant = [p for p in candidates if not any(p["valid_from"] >= old["valid_from"] and p["valid_to"] <= old["valid_to"] for old in informed)]
            if not relevant:
                continue
            current = {**weather, "periods": relevant}
            now = self.clock.now().timestamp()
            expires = min(now + 1800, min(parse_time(p["valid_to"]).timestamp() for p in relevant))
            await self.prepare_push(user, f"alert:{user['id']}:{self.clock.now().date()}", "weather_alert", current, now, expires)

    async def send_due(self):
        now = self.clock.now().timestamp()
        rows = await self.db.all("SELECT * FROM outbox WHERE method='push' AND status='pending' AND send_at<=? ORDER BY id", (now,))
        for row in rows:
            user = await self.db.one("SELECT * FROM users WHERE id=?", (row["user_id"],))
            metadata = json.loads(row["weather"])
            allowed = user["active"] and row["expires_at"] > now and metadata["location_code"] == user["location_code"]
            if row["kind"] == "morning_brief":
                allowed = allowed and user["morning_enabled"] and metadata["wake_time"] == user["wake_time"]
            else:
                local = self.clock.now()
                awake = user["night_forecast_enabled"] if is_night(local) else local.strftime("%H:%M") >= user["wake_time"]
                allowed = allowed and user["alerts_enabled"] and awake
            if not allowed:
                await self.db.execute("UPDATE outbox SET status='cancelled' WHERE id=?", (row["id"],))
                continue
            await self.db.execute("UPDATE outbox SET status='sending',attempts=attempts+1 WHERE id=?", (row["id"],))
            try:
                await self.sender.push(row["user_id"], json.loads(row["payload"]), row["retry_key"])
            except Exception as exc:
                retry = isinstance(exc, (httpx.TransportError, TimeoutError)) or isinstance(exc, httpx.HTTPStatusError) and (exc.response.status_code >= 500 or exc.response.status_code == 429)
                await self.db.execute("UPDATE outbox SET status=?,send_at=?,error=? WHERE id=?", ("pending" if retry else "failed", now + 30, type(exc).__name__, row["id"]))
                continue
            async with self.db.transaction() as conn:
                await conn.execute("UPDATE outbox SET status='sent',sent_at=? WHERE id=?", (self.clock.now().timestamp(), row["id"]))
                await conn.execute("UPDATE conversation_state SET choices=?,choice_set_id=?,choices_expires=? WHERE user_id=?", (dumps(metadata["choices"]), metadata["choice_set_id"], now + 86400, row["user_id"]))
                await conn.execute("INSERT INTO messages(user_id,role,content,kind,created_at) VALUES (?,'assistant',?,?,?)", (row["user_id"], json.loads(row["payload"])["messages"][0]["text"], row["kind"], now))
