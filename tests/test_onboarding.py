import asyncio

import pytest

from conftest import chat
from weather_bot.onboarding import onboarding_reminder


@pytest.mark.parametrize("prefix,step", [
    ([], "region"),
    (["新北市"], "wake"),
    (["新北市", "07:00"], "morning"),
    (["新北市", "07:00", "開啟晨報"], "night"),
    (["新北市", "07:00", "開啟晨報", "起床後再提醒"], "preferences"),
    (["新北市", "07:00", "開啟晨報", "起床後再提醒", "先跳過"], "confirm"),
], ids=["region", "wake", "morning", "night", "preferences", "confirm"])
@pytest.mark.parametrize("input_kind", ["text", "number", "postback"])
async def test_unfinished_onboarding_reminds_current_question_via_fixed_reply(bot, prefix, step, input_kind):
    for text in prefix:
        await chat(bot, text)
    event = await chat(bot, "1" if input_kind == "number" else "今天心情如何？",
                       postback="choice_set_id=old&choice_id=2" if input_kind == "postback" else None)
    payload = bot.line.replies[-1]["payload"]
    assert len(payload["messages"]) == 1
    message = payload["messages"][0]
    assert set(message) == {"type", "text"}
    assert message["type"] == "text"
    assert onboarding_reminder(step) in message["text"]
    assert "None" not in message["text"] and "補行政區" not in message["text"]
    assert "過期" not in message["text"]
    assert (await bot.db.one("SELECT setup_step FROM users"))["setup_step"] == step
    assert bot.model.calls == [] and bot.line.pushes == []
    assert await bot.db.all("SELECT id FROM jobs WHERE kind!='chat'") == []
    assert await bot.db.all("SELECT id FROM messages WHERE kind='chat'") == []
    assert (await bot.db.one("SELECT method,kind FROM outbox ORDER BY id DESC LIMIT 1")) == {"method": "reply", "kind": "setting"}
    replies = len(bot.line.replies)
    await bot.receive(event)
    await bot.drain_chats()
    assert len(bot.line.replies) == replies


@pytest.mark.parametrize("command", ["修改起床時間", "開啟夜間提醒"], ids=["wake-command", "night-command"])
async def test_unfinished_user_cannot_skip_region_with_setting_command(bot, command):
    await chat(bot, command)
    assert (await bot.db.one("SELECT setup_step FROM users"))["setup_step"] == "region"
    assert onboarding_reminder("region") in bot.line.replies[-1]["payload"]["messages"][0]["text"]
    assert bot.model.calls == []


async def test_non_text_message_and_follow_get_fixed_onboarding_reminder(bot):
    for kind in ("message", "follow"):
        token = f"onboarding-{kind}"
        bot.line.issue(token)
        await bot.receive({"type": kind, "webhookEventId": token, "replyToken": token,
                           "source": {"type": "user", "userId": "Utest"},
                           "message": {"type": "sticker", "stickerId": "1"}})
        await bot.drain_chats()
        assert onboarding_reminder("region") in bot.line.replies[-1]["payload"]["messages"][0]["text"]
    assert len(bot.line.replies) == 2
    assert bot.model.calls == [] and bot.line.pushes == []


async def test_queued_onboarding_timeout_keeps_fixed_reminder(bot):
    await bot.db.ensure_user("Utest")
    bot.settings.chat_timeout = 0.01
    lock = bot.user_locks["Utest"] = asyncio.Lock()
    await lock.acquire()
    try:
        await chat(bot, "你好")
    finally:
        lock.release()
    message = bot.line.replies[-1]["payload"]["messages"][0]
    assert set(message) == {"type", "text"}
    assert onboarding_reminder("region") in message["text"]
    assert bot.model.calls == [] and bot.line.pushes == []


async def test_forecast_failure_does_not_block_valid_county(bot, monkeypatch):
    async def unavailable(*args):
        raise ConnectionError("mock CWA unavailable")
    monkeypatch.setattr(bot.weather, "refresh", unavailable)
    await chat(bot, "新北市")
    text = bot.line.replies[-1]["payload"]["messages"][0]["text"]
    assert "新北市" in text and "None" not in text
    assert onboarding_reminder("wake") in text
    assert (await bot.db.one("SELECT setup_step FROM users"))["setup_step"] == "wake"
    assert bot.model.calls == []
