"""LINE 事件、固定設定與具有絕對期限的聊天處理。"""

import asyncio
from datetime import timedelta
import json
import re
import time
from urllib.parse import parse_qs
import uuid

from weather_bot.chat import fallback
from weather_bot.db import dumps
from weather_bot.line_io import fixed_payload, message_payload
from weather_bot.onboarding import collect_setup, current_draft, initial_draft, setup_reply, setup_summary
from weather_bot.regions import CITIES, city_from_text


def requested_weather_range(text, now):
    day = now + timedelta(days=1) if "明天" in text else now + timedelta(days=2) if "後天" in text else now
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    if "下午" in text:
        return start + timedelta(hours=12), start + timedelta(hours=18)
    if "早上" in text or "上午" in text:
        return start + timedelta(hours=6), start + timedelta(hours=12)
    if "晚上" in text or "今晚" in text:
        return start + timedelta(hours=18), start + timedelta(days=1)
    return (start if day.date() != now.date() else now), start + timedelta(days=1)


async def receive(bot, event, received_at):
    source = event.get("source", {})
    if source.get("type") != "user" or not source.get("userId") or not event.get("webhookEventId"):
        return
    user_id = source["userId"]
    await bot.db.ensure_user(user_id)
    kind = event.get("type")
    if kind == "unfollow":
        await bot.db.execute("UPDATE users SET active=0 WHERE id=?", (user_id,))
        return
    if kind not in {"message", "postback", "follow"} or not event.get("replyToken"):
        return
    if kind == "message" and event.get("message", {}).get("type") != "text":
        user = await bot.db.one("SELECT setup_step FROM users WHERE id=?", (user_id,))
        if user["setup_step"] == "done":
            return
    await bot.db.job("chat", user_id, f"line:{event['webhookEventId']}", event, received_at,
                     deadline=received_at + bot.settings.reply_timeout)
    bot.wakeup.set()


async def setting(bot, user, text, event):
    uid = user["id"]
    step = user["setup_step"]
    if step != "done":
        return await collect_setup(bot, user, text, event)
    if text in {"設定", "查看設定", "取消修改"}:
        draft = await current_draft(bot, user)
        await bot.db.execute("UPDATE users SET setup_data=? WHERE id=?", (dumps(draft), uid))
        return "目前設定 (｡･ω･｡)：\n" + setup_summary(draft) + "\n可直接說要改的項目，例如「晨報時間改成早上八點半」。"
    if text in {"修改地區", "修改起床時間"}:
        draft = await current_draft(bot, user)
        for key in (("wake_time",) if text == "修改起床時間" else ("city",)):
            draft.pop(key, None)
        draft["_editing"] = True
        await bot.db.execute("UPDATE users SET setup_data=? WHERE id=?", (dumps(draft), uid))
        return setup_reply(draft)
    if text in {"開啟天氣提醒", "關閉天氣提醒"}:
        await bot.db.execute("UPDATE users SET alerts_enabled=? WHERE id=?", (text.startswith("開啟"), uid))
        return "設定已更新。\n" + setup_summary(await current_draft(bot, await bot.db.one("SELECT * FROM users WHERE id=?", (uid,))))
    setting_request = re.search(
        r"(?:修改|更正).{0,8}(?:地區|縣市|時間|晨報|夜間|稱呼|口吻)|(?:地區|縣市|時間|晨報|夜間|稱呼|口吻).{0,8}(?:改|開|關)"
        r"|改住|搬到|(?:開啟|關閉)(?:晨報|夜間提醒)|^(?:請)?(?:叫我|稱呼我|以後叫我)|夜間也提醒|起床後再提醒"
        r"|晨報.*(?:要|不要|不用)|夜間.*(?:要|不要|不用)", text)
    if initial_draft(user).get("_editing") or (setting_request and not re.search(r"[？?]|怎麼|如何", text)):
        return await collect_setup(bot, user, text, event, editing=True)
    return None


async def resolve_input(bot, user_id, event):
    text = event.get("message", {}).get("text", "")
    postback = event.get("postback", {}).get("data")
    if text not in {"1", "2", "3"} and postback is None:
        return text, False
    state = await bot.db.one("SELECT * FROM conversation_state WHERE user_id=?", (user_id,))
    choice_id = text
    if postback is not None:
        params = parse_qs(postback)
        if params.get("choice_set_id") != [state["choice_set_id"]]:
            return "這組選項已過期，請選最新選項或直接輸入文字。", True
        choice_id = params.get("choice_id", [""])[0]
    if not state["choices"] or (state["choices_expires"] or 0) <= bot.clock.now().timestamp():
        return "這組選項已過期，請直接輸入想聊的內容。", True
    choices = json.loads(state["choices"])
    choice = next((c for c in choices if c["id"] == choice_id), None)
    return (choice["user_text"], False) if choice else ("找不到這個選項，請直接輸入文字。", True)


async def process_chat(bot, job):
    lock = bot.user_locks.setdefault(job["user_id"], asyncio.Lock())
    remaining = max(0.001, job["created_at"] + bot.settings.chat_timeout - bot.clock.now().timestamp())
    started = time.monotonic()
    acquired = False
    try:
        async with asyncio.timeout(remaining):
            await lock.acquire()
            acquired = True
    except TimeoutError:
        pass
    try:
        await _process_chat(bot, job, processing_remaining=max(0.001, remaining - (time.monotonic() - started)), skip_model=not acquired)
    finally:
        if acquired:
            lock.release()


async def _process_chat(bot, job, *, processing_remaining, skip_model):
    now = bot.clock.now().timestamp()
    event = json.loads(job["payload"])
    uid = job["user_id"]
    if now >= job["deadline"]:
        await bot.finish_job(job["id"], "expired")
        return
    reply, context = fallback(), {}
    generation_error = None
    payload, text, is_chat = None, event.get("message", {}).get("text", ""), False
    # 排隊也算在 25 秒處理期限內，保留至少 30 秒供 LINE 傳送與固定回覆。
    remaining = min(processing_remaining, max(0.001, job["created_at"] + bot.settings.chat_timeout - now))
    try:
        async with asyncio.timeout(remaining):
            if not skip_model:
                if event["type"] == "follow":
                    await bot.db.execute("UPDATE users SET active=1 WHERE id=?", (uid,))
                user = await bot.db.one("SELECT * FROM users WHERE id=?", (uid,))
                if not user["active"]:
                    await bot.finish_job(job["id"], "cancelled")
                    return
                if user["setup_step"] != "done":
                    # 未完成引導時優先解析設定；編號、舊按鈕與非文字訊息皆使用固定提示。
                    fixed = await setting(bot, user, text, event)
                else:
                    text, expired = await resolve_input(bot, uid, event)
                    fixed = text if expired else await setting(bot, user, text, event)
                if fixed is not None:
                    payload = fixed_payload(fixed)
                elif len(text) > 2000:
                    payload = fixed_payload("這段有點長，請拆成較短的訊息再跟我聊吧。")
                else:
                    is_chat = True
                    weather = None
                    if re.search(r"天氣|下雨|降雨|氣溫|幾度|冷不冷", text):
                        try:
                            city = city_from_text(text) or user["city"]
                        except ValueError as exc:
                            payload = fixed_payload(str(exc))
                        else:
                            weather = await bot.weather.get_weather({**user, "city": city, "location_code": CITIES[city]}, requested_weather_range(text, bot.clock.now()))
                    if payload is None:
                        reply, context = await bot.chat.generate(user, text, weather=weather)
    except Exception as exc:
        # 僅記錄錯誤類型於 job；不可把含 API URL/金鑰的例外字串寫進 log。
        generation_error = type(exc).__name__
    choice_id = uuid.uuid4().hex
    user = await bot.db.one("SELECT * FROM users WHERE id=?", (uid,))
    if payload is None:
        payload = fixed_payload(setup_reply(initial_draft(user))) if user["setup_step"] != "done" else message_payload(reply, choice_id)
    now = bot.clock.now().timestamp()
    if not user["active"]:
        await bot.finish_job(job["id"], "cancelled")
        return
    if now >= job["deadline"]:
        await bot.finish_job(job["id"], "expired")
        return
    outbox_id = await bot.db.execute("INSERT INTO outbox(user_id,business_key,kind,method,payload,memory_ids,send_at,expires_at,status,attempts) VALUES (?,?,?,'reply',?,?,?,?,'sending',1)",
        (uid, job["business_key"], "chat" if is_chat else "setting", dumps(payload), dumps([m["id"] for m in context.get("memories", [])]), now, job["deadline"]))
    try:
        async with asyncio.timeout(max(0.001, job["deadline"] - now)):
            await bot.sender.reply(event["replyToken"], payload)
    except Exception as exc:
        await bot.db.execute("UPDATE outbox SET status='uncertain',error=? WHERE id=?", (type(exc).__name__, outbox_id))
        await bot.finish_job(job["id"], "uncertain")
        return  # 一次性 token 不重試，也不改用 Push。
    sent = bot.clock.now().timestamp()
    async with bot.db.transaction() as conn:
        await conn.execute("UPDATE outbox SET status='sent',sent_at=? WHERE id=?", (sent, outbox_id))
        if is_chat:
            async with conn.execute("SELECT COALESCE(MAX(turn),0)+1 FROM messages WHERE user_id=?", (uid,)) as cur:
                turn = (await cur.fetchone())[0]
            # 接收可能比前輪送出更早；升為 chat 時排到尾端，維持每輪 user/assistant 的 ID 順序。
            await conn.execute("""INSERT INTO messages(user_id,role,content,turn,created_at,external_event_id,event_timestamp) VALUES (?,'user',?,?,?,?,?)
                ON CONFLICT(external_event_id) DO UPDATE SET id=CASE WHEN messages.id<(SELECT MAX(id) FROM messages) THEN excluded.id ELSE messages.id END,
                content=excluded.content,kind='chat',turn=excluded.turn
                WHERE messages.user_id=excluded.user_id AND messages.kind='received'""",
                (uid, text, turn, job["created_at"], event["webhookEventId"], event.get("timestamp", 0) / 1000))
            await conn.execute("INSERT INTO messages(user_id,role,content,turn,created_at) VALUES (?,'assistant',?,?,?)", (uid, "\n".join(reply.sentences), turn, sent))
            await conn.execute("UPDATE conversation_state SET choices=?,choice_set_id=?,choices_expires=? WHERE user_id=?", (dumps([c.model_dump() for c in reply.choices]), choice_id, sent + 86400, uid))
    await bot.finish_job(job["id"], "done", generation_error)
    if is_chat:
        await bot.queue_memory(uid, urgent=bool(re.search(r"記住|更正|忘記|以後|改成|溫柔|叫我", text)))
