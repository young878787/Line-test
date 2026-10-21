"""獨立檢查所發現的邊界回歸。"""
from datetime import timedelta
import json
import pytest

from conftest import chat, configured, LineMock, TestClock
from weather_bot.chat import weather_facts


async def test_reschedule_prepared_morning_same_day(bot):
    await configured(bot)
    await bot.tick()
    await bot.run_background()
    await bot.db.execute("UPDATE users SET wake_time='08:00'")
    bot.clock.advance(600)
    await bot.send_due()
    bot.clock.advance(3000)
    await bot.tick()
    await bot.run_background()
    bot.clock.advance(600)
    await bot.send_due()
    assert len(bot.line.pushes) == 1
    assert len(await bot.db.all("SELECT id FROM outbox WHERE kind='morning_brief'")) == 1


async def test_morning_budget_fallback_still_six_and_cross_midnight_date(bot):
    user = await configured(bot)
    bot.settings.input_budget = 1
    bot.clock.value = bot.clock.value.replace(hour=23, minute=55)
    target = (bot.clock.now() + timedelta(days=1)).replace(hour=0, minute=5)
    await bot.db.execute("UPDATE users SET wake_time='00:05'")
    await bot.prepare_morning(user["id"], target)
    row = await bot.db.one("SELECT payload FROM outbox WHERE kind='morning_brief'")
    text = json.loads(row["payload"])["messages"][0]["text"].split("\n\n")[0]
    assert len(text.splitlines()) == 6 and "2026-10-22" in text


async def test_alert_retry_rechecks_daytime_wake(bot):
    user = await configured(bot)
    bot.clock.value = bot.clock.value.replace(hour=5, minute=25)
    await bot.db.execute("UPDATE users SET night_forecast_enabled=1")
    user = await bot.db.one("SELECT * FROM users")
    weather = await bot.weather.get_weather(user)
    await bot.prepare_push(user, "night", "weather_alert", weather, bot.clock.now().timestamp(), bot.clock.now().timestamp()+1800)
    bot.clock.advance(600)
    await bot.send_due()
    assert bot.line.pushes == []


async def test_alert_adjacent_candidates_include_earliest_event(bot):
    user = await configured(bot)
    weather = await bot.weather.get_weather(user)
    selected = {**weather, "periods": weather["periods"][:3]}
    for p in selected["periods"]:
        p.update(rain_state="rain", pop_3h=70)
    facts = weather_facts(user, selected, "weather_alert", bot.clock.now())
    assert "10-21 06:00至15:00" in facts[0]
    await bot.prepare_push(user, "merged", "weather_alert", selected, bot.clock.now().timestamp(), bot.clock.now().timestamp()+1800)
    row = await bot.db.one("SELECT weather FROM outbox WHERE business_key='merged'")
    assert json.loads(row["weather"])["periods"] == [{"valid_from": selected["periods"][0]["valid_from"], "valid_to": selected["periods"][2]["valid_to"]}]


async def test_permanent_forget_cursor_survives_new_value_and_compaction(bot):
    await configured(bot)
    await chat(bot, "朋友都叫我海鹽布丁，我也比較喜歡這個稱呼。")
    await bot.memory.organize("Utest")
    await chat(bot, "忘記我的稱呼")
    original = bot.model.generate
    async def forget(system, data, *, memory=False):
        return {"forget_keys": ["persona.address_user_as"]} if memory else await original(system, data)
    bot.model.generate = forget
    await bot.memory.organize("Utest")
    state = await bot.db.one("SELECT * FROM conversation_state")
    assert state["forgotten_through"] == 4
    bot.model.generate = original
    await chat(bot, "朋友都叫我海鹽布丁，我也比較喜歡這個稱呼。")
    await bot.memory.organize("Utest")
    assert (await bot.db.one("SELECT forgotten FROM memories"))["forgotten"] == 0
    context = await bot.chat.context("Utest")
    assert all(m["id"] > 4 for m in context["recent"])
    for i in range(17):
        await chat(bot, f"後續第{i}件事")
    await bot.memory.organize("Utest", compact=True)
    compact = next(c["data"] for c in reversed(bot.model.calls) if c["memory"] and c["data"]["compact"])
    assert all(m["id"] > 4 for m in compact["messages"])


async def test_line_mock_rejects_used_and_sixty_second_token():
    clock = TestClock()
    sender = LineMock(clock)
    sender.issue("once")
    await sender.reply("once", {"messages": []})
    with pytest.raises(AssertionError, match="已使用"):
        await sender.reply("once", {"messages": []})
    sender.issue("expired")
    clock.advance(60)
    with pytest.raises(AssertionError, match="逾期"):
        await sender.reply("expired", {"messages": []})


async def test_same_user_serial_context_and_queue_time_in_deadline(bot):
    await configured(bot)
    bot.model.delay = 0.05
    for i in range(3):
        token = f"queued{i}"
        bot.line.issue(token)
        event = {"type": "message", "webhookEventId": token, "replyToken": token,
                 "source": {"type": "user", "userId": "Utest"}, "message": {"type": "text", "text": f"聊天{i}"}}
        await bot.receive(event)
    await bot.drain_chats()
    assert len(bot.line.replies) == 3
    assert len(bot.model.calls[1]["data"]["recent"]) == 2
    assert len(bot.model.calls[2]["data"]["recent"]) == 4
    assert max(r["elapsed_seconds"] for r in bot.line.replies) < 60
