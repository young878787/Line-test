import asyncio

import pytest

from conftest import chat


@pytest.mark.parametrize("prefix,step,remaining", [
    ([], "region", "天氣地區、起床時間、晨報與一般天氣提醒、夜間預報提醒、確認初始設定與偏好"),
    (["新北市板橋區"], "wake", "起床時間、晨報與一般天氣提醒、夜間預報提醒、確認初始設定與偏好"),
    (["新北市板橋區", "07:00"], "notifications", "晨報與一般天氣提醒、夜間預報提醒、確認初始設定與偏好"),
    (["新北市板橋區", "07:00", "全部開啟"], "night", "夜間預報提醒、確認初始設定與偏好"),
    (["新北市板橋區", "07:00", "全部開啟", "保持關閉"], "confirm", "確認初始設定與偏好"),
], ids=["region", "wake", "notifications", "night", "confirm"])
@pytest.mark.parametrize("input_kind", ["text", "number", "postback"])
async def test_unfinished_onboarding_reminds_remaining_via_fixed_reply(bot, prefix, step, remaining, input_kind):
    for text in prefix:
        await chat(bot, text)
    event = await chat(bot, "1" if input_kind == "number" else "今天心情如何？",
                       postback="choice_set_id=old&choice_id=2" if input_kind == "postback" else None)
    payload = bot.line.replies[-1]["payload"]
    assert len(payload["messages"]) == 1
    message = payload["messages"][0]
    assert set(message) == {"type", "text"}
    assert message["type"] == "text"
    assert message["text"].startswith("新手引導尚未完成。\n")
    assert f"待完成：{remaining}。" in message["text"]
    assert "下一步：" in message["text"]
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
    assert "待完成：天氣地區、起床時間" in bot.line.replies[-1]["payload"]["messages"][0]["text"]
    assert bot.model.calls == []


async def test_non_text_message_and_follow_get_fixed_onboarding_reminder(bot):
    for kind in ("message", "follow"):
        token = f"onboarding-{kind}"
        bot.line.issue(token)
        await bot.receive({"type": kind, "webhookEventId": token, "replyToken": token,
                           "source": {"type": "user", "userId": "Utest"},
                           "message": {"type": "sticker", "stickerId": "1"}})
        await bot.drain_chats()
        assert "新手引導尚未完成" in bot.line.replies[-1]["payload"]["messages"][0]["text"]
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
    assert message["text"].startswith("新手引導尚未完成。")
    assert bot.model.calls == [] and bot.line.pushes == []


async def test_region_lookup_failure_keeps_remaining_steps(bot, monkeypatch):
    async def unavailable(*args):
        raise ConnectionError("mock CWA unavailable")
    monkeypatch.setattr(bot.weather, "refresh", unavailable)
    await chat(bot, "新北市板橋區")
    text = bot.line.replies[-1]["payload"]["messages"][0]["text"]
    assert "暫時無法確認這個行政區" in text
    assert "待完成：天氣地區、起床時間" in text
    assert (await bot.db.one("SELECT setup_step FROM users"))["setup_step"] == "region"
    assert bot.model.calls == []
