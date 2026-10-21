import asyncio
import base64
import hashlib
import hmac
import json
import time

import httpx
import pytest

from conftest import chat, configured
from weather_bot.line_io import LineSender
from weather_bot.main import create_app


async def test_reply_token_once_deadline_excludes_slow_memory(bot, record_property):
    await configured(bot)
    bot.model.memory_delay = 2
    await chat(bot, "朋友都叫我海鹽布丁，我也比較喜歡這個稱呼。")
    bot.clock.advance(121)
    await bot.tick()
    background = asyncio.create_task(bot.run_background())
    await asyncio.sleep(0.05)
    start = time.monotonic()
    event = await chat(bot, "現在能聊天嗎")
    elapsed = time.monotonic() - start
    assert elapsed < 1 and not background.done()
    await bot.receive(event)
    await bot.drain_chats()
    assert len(bot.line.replies) == 2
    assert max(r["elapsed_seconds"] for r in bot.line.replies) < 60
    record_property("reply_elapsed_seconds", elapsed)
    record_property("background_memory_still_running_at_reply", not background.done())
    record_property("max_token_age_seconds", max(r["elapsed_seconds"] for r in bot.line.replies))
    await background


async def test_slow_model_fallback_uses_reply_not_push_under_one_minute(bot):
    await configured(bot)
    bot.settings.chat_timeout = 0.05
    bot.model.delay = 1
    await chat(bot, "聊聊天")
    assert len(bot.line.replies) == 1 and bot.line.pushes == []
    assert "迷糊" in bot.line.replies[0]["payload"]["messages"][0]["text"]
    assert bot.line.replies[0]["elapsed_seconds"] < 60


async def test_expired_queued_event_not_replied_or_pushed(bot):
    await configured(bot)
    event = {"type": "message", "webhookEventId": "expired", "replyToken": "expired",
             "source": {"type": "user", "userId": "Utest"}, "message": {"type": "text", "text": "你好"}}
    bot.line.issue("expired")
    await bot.receive(event)
    bot.clock.advance(60)
    await bot.drain_chats()
    assert bot.line.replies == bot.line.pushes == []
    assert (await bot.db.one("SELECT status FROM jobs WHERE kind='chat'"))["status"] == "expired"


async def test_line_http_adapter_reply_payload_retry_key_and_409():
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(409, headers={"x-line-accepted-request-id": "accepted"}) if request.url.path.endswith("push") else httpx.Response(200, json={})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        sender = LineSender("fake", http)
        payload = {"messages": [{"type": "text", "text": "你好"}]}
        await sender.reply("once", payload)
        await sender.push("user", payload, "retry-id")
    assert json.loads(requests[0].content)["replyToken"] == "once"
    assert requests[1].headers["X-Line-Retry-Key"] == "retry-id"
    assert len(json.loads(requests[0].content)["messages"]) == 1


async def test_webhook_signature_durable_enqueue_and_restart_dedupe(bot):
    app = create_app(bot=bot)
    app.state.bot = bot
    event = {"type": "message", "webhookEventId": "webhook", "replyToken": "webhook",
             "source": {"type": "user", "userId": "Utest"}, "message": {"type": "text", "text": "你好"}}
    body = json.dumps({"events": [event]}).encode()
    signature = base64.b64encode(hmac.new(b"test-secret", body, hashlib.sha256).digest()).decode()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post("/webhook", content=body, headers={"x-line-signature": "bad"})).status_code == 400
        assert (await client.post("/webhook", content=body, headers={"x-line-signature": signature})).status_code == 200
        assert bot.line.replies == []
        await bot.db.close()
        await bot.db.open()
        await bot.db.recover()
        await client.post("/webhook", content=body, headers={"x-line-signature": signature})
    assert len(await bot.db.all("SELECT * FROM jobs WHERE kind='chat'")) == 1


async def test_restart_does_not_reuse_claimed_token_and_retains_input(bot):
    await configured(bot)
    event = {"type": "message", "webhookEventId": "claimed", "replyToken": "secret-token",
             "source": {"type": "user", "userId": "Utest"}, "message": {"type": "text", "text": "保留這句原始輸入"}}
    await bot.receive(event)
    await bot.db.execute("UPDATE jobs SET status='running' WHERE kind='chat'")
    await bot.db.recover()
    await bot.drain_chats()
    job = await bot.db.one("SELECT * FROM jobs WHERE kind='chat'")
    assert job["status"] == "uncertain" and job["payload"] == "{}"
    assert bot.line.replies == []
    assert (await bot.db.one("SELECT * FROM messages"))["content"] == "保留這句原始輸入"
