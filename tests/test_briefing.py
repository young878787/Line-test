from datetime import timedelta
import json

from conftest import configured


async def test_T02_scheduled_morning_without_new_forecast_six_sentences_once(bot):
    await configured(bot)
    await bot.tick()
    await bot.run_background()
    assert bot.line.pushes == []  # 06:50 準備，尚未到 07:00。
    rows = await bot.db.all("SELECT * FROM outbox WHERE kind='morning_brief'")
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload"])
    assert len(payload["messages"]) == 1
    assert len(payload["messages"][0]["text"].split("\n\n")[0].splitlines()) == 6
    assert "新北市" in payload["messages"][0]["text"]
    assert "板橋區" not in payload["messages"][0]["text"] and "None" not in payload["messages"][0]["text"]
    assert "23°C" in payload["messages"][0]["text"]
    bot.clock.advance(600)
    await bot.tick()
    await bot.run_background()
    await bot.send_due()
    await bot.tick()
    await bot.run_background()
    await bot.send_due()
    assert len(bot.line.pushes) == 1
    assert len(await bot.db.all("SELECT * FROM weather_snapshots")) == 1


async def test_cross_midnight_late_skip_and_settings_recheck(bot):
    user = await configured(bot)
    bot.clock.value = bot.clock.value.replace(hour=23, minute=55)
    await bot.db.execute("UPDATE users SET wake_time='00:05' WHERE id=?", (user["id"],))
    await bot.tick()
    await bot.run_background()
    row = await bot.db.one("SELECT * FROM outbox WHERE kind='morning_brief'")
    assert row["business_key"].endswith("2026-10-22")
    bot.clock.advance(600)
    await bot.db.execute("UPDATE users SET morning_enabled=0 WHERE id=?", (user["id"],))
    await bot.send_due()
    assert bot.line.pushes == []
    assert (await bot.db.one("SELECT status FROM outbox"))["status"] == "cancelled"
    # 遲到超過 20 分鐘不補送。
    await bot.db.execute("UPDATE users SET morning_enabled=1 WHERE id=?", (user["id"],))
    bot.clock.advance(86400 + 1800)
    await bot.tick()
    assert len(await bot.db.all("SELECT * FROM jobs WHERE kind='morning'")) == 1
