"""逐題設定、口語更正及預報變更到送達的閉環回歸。"""

from copy import deepcopy
import asyncio
import json

import pytest

from conftest import chat, configured
from weather_bot.regions import CITIES, DATASET


def change_weather(raw, index, code="07", text="陰天"):
    row = raw["records"]["Locations"][0]["Location"][0]["WeatherElement"][0]["Time"][index]
    row["ElementValue"][0].update(WeatherCode=code, Weather=text)


async def test_short_answers_correction_skip_and_confirm(bot):
    for text in ("臺中市", "早上七點半", "要", "時間改成早上八點", "不要", "先跳過"):
        await chat(bot, text)
    user = await bot.db.one("SELECT * FROM users")
    draft = json.loads(user["setup_data"])
    assert user["setup_step"] == "confirm" and draft["wake_time"]["value"] == "08:00"
    assert draft["morning_enabled"]["value"] is True and draft["night_forecast_enabled"]["value"] is False
    assert user["city"] is None and await bot.db.all("SELECT * FROM memories") == []
    await chat(bot, "確認開始")
    user = await bot.db.one("SELECT * FROM users")
    assert (user["city"], user["wake_time"], user["alerts_enabled"], user["setup_step"]) == ("臺中市", "08:00", 1, "done")
    assert bot.model.calls == [] and bot.line.pushes == []


async def test_completed_settings_edit_updates_schedule_and_preserves_notices(bot):
    await configured(bot)
    await bot.tick()
    await bot.run_background()
    await chat(bot, "設定")
    assert (await bot.db.one("SELECT setup_step FROM users"))["setup_step"] == "done"
    await chat(bot, "晨報時間改成早上八點半")
    assert (await bot.db.one("SELECT wake_time FROM users"))["wake_time"] == "08:30"
    assert (await bot.db.one("SELECT status FROM outbox WHERE kind='morning_brief'"))["status"] == "cancelled"
    await chat(bot, "改住澎湖縣")
    assert (await bot.db.one("SELECT city,location_code FROM users")) == {"city": "澎湖縣", "location_code": CITIES["澎湖縣"]}
    await chat(bot, "晨報還是不要好了")
    user = await bot.db.one("SELECT * FROM users")
    assert user["morning_enabled"] == 0 and user["alerts_enabled"] == 1 and user["setup_step"] == "done"
    await chat(bot, "叫我阿洋")
    assert (await bot.db.one("SELECT value FROM memories WHERE key='persona.address_user_as'"))["value"] == "阿洋"
    await chat(bot, "今天好累")
    assert (await bot.db.one("SELECT kind FROM outbox ORDER BY id DESC LIMIT 1"))["kind"] == "chat"


async def test_natural_night_consent_and_name_corrections(bot):
    await configured(bot)
    for text, enabled in (("夜間還是可以提醒我", 1), ("夜間不可以提醒我", 0)):
        await chat(bot, text)
        assert (await bot.db.one("SELECT night_forecast_enabled FROM users"))["night_forecast_enabled"] == enabled
    await chat(bot, "改叫我阿洋")
    assert (await bot.db.one("SELECT value FROM memories WHERE key='persona.address_user_as'"))["value"] == "阿洋"
    assert bot.model.calls == []


async def test_noop_or_invalid_edit_does_not_trap_following_chat(bot):
    await configured(bot)
    for text in ("開啟晨報", "時間改成香蕉"):
        await chat(bot, text)
        assert not json.loads((await bot.db.one("SELECT setup_data FROM users"))["setup_data"]).get("_editing")
        await chat(bot, "今天辛苦了")
        assert (await bot.db.one("SELECT kind FROM outbox ORDER BY id DESC LIMIT 1"))["kind"] == "chat"


@pytest.mark.parametrize("bad", ["invented_value", "unknown_field", "wrong_type"])
async def test_partial_model_changes_keep_valid_values_without_unsafe_updates(bot, monkeypatch, bad):
    async def extract(system, data, **kwargs):
        source = data["messages"][-1]
        invalid = {"key": "wake_time", "value": "09:00", "source_message_id": source["id"], "quote": "7:30起床"}
        if bad == "unknown_field":
            invalid["key"] = "district"
        elif bad == "wrong_type":
            invalid["value"] = 730
        return {"updates": [
            {"key": "city", "value": "臺中市", "source_message_id": source["id"], "quote": "臺中市"},
            invalid]}
    monkeypatch.setattr(bot.ai, "generate", extract)
    await chat(bot, "臺中市，7:30起床")
    draft = json.loads((await bot.db.one("SELECT setup_data FROM users"))["setup_data"])
    assert draft["city"]["value"] == "臺中市" and "wake_time" not in draft
    assert "其他有效答案會保留" in bot.line.replies[-1]["payload"]["messages"][0]["text"]


async def test_time_question_cannot_become_an_update(bot, monkeypatch):
    await chat(bot, "臺中市")
    async def extract(system, data, **kwargs):
        source = data["messages"][-1]
        return {"updates": [{"key": "wake_time", "value": "07:30", "source_message_id": source["id"], "quote": source["content"]}]}
    monkeypatch.setattr(bot.ai, "generate", extract)
    await chat(bot, "如果早上7:30起床可以嗎？")
    assert "wake_time" not in json.loads((await bot.db.one("SELECT setup_data FROM users"))["setup_data"])


async def test_chat_cache_refresh_creates_durable_notice_and_restart_recovers(bot, raw_weather):
    await configured(bot)
    bot.clock.advance(3 * 3600 + 600)
    change_weather(raw_weather, 2)
    await bot.weather.refresh(DATASET, "新北市")
    assert (await bot.db.one("SELECT status FROM jobs WHERE kind='weather_change'"))["status"] == "pending"
    await bot.db.close()
    await bot.db.open()
    await bot.db.recover()
    await bot.run_background()
    await bot.send_due()
    assert len(bot.line.pushes) == 1
    text = bot.line.pushes[0][1]["messages"][0]["text"]
    assert "預報時段變更" in text and "陰天" in text and "新北市" in text
    await bot.poll_weather(DATASET, "新北市")
    await bot.send_due()
    assert len(bot.line.pushes) == 1


async def test_distinct_changes_same_period_same_day_each_notify(bot, raw_weather):
    await configured(bot)
    bot.clock.advance(3 * 3600 + 600)
    for code, text in (("07", "陰天"), ("08", "短暫雨"), ("02", "晴時多雲")):
        change_weather(raw_weather, 2, code, text)
        await bot.poll_weather(DATASET, "新北市")
        await bot.send_due()
    assert len(bot.line.pushes) == 3
    assert len({row[2] for row in bot.line.pushes}) == 3
    assert len(await bot.db.all("SELECT id FROM jobs WHERE kind='weather_change' AND status='done'")) == 3


@pytest.mark.parametrize("period,alerts", [(8, 0), (9, 1)])
async def test_night_deferral_morning_suppresses_only_displayed_fields(bot, raw_weather, period, alerts):
    await configured(bot)
    bot.clock.value = bot.clock.value.replace(hour=23, minute=40)
    change_weather(raw_weather, period)
    await bot.poll_weather(DATASET, "新北市")
    assert bot.line.pushes == []
    assert (await bot.db.one("SELECT status,error FROM jobs WHERE kind='weather_change'")) == {"status": "pending", "error": "deferred_quiet_or_quota"}
    bot.clock.advance(7 * 3600 + 10 * 60)
    await bot.tick()
    await bot.run_background()
    bot.clock.advance(10 * 60)
    await bot.send_due()
    await bot.run_background()
    await bot.send_due()
    assert len(await bot.db.all("SELECT id FROM outbox WHERE kind='morning_brief' AND status='sent'")) == 1
    assert len(await bot.db.all("SELECT id FROM outbox WHERE kind='weather_alert' AND status='sent'")) == alerts


async def test_night_changes_revert_to_baseline_no_stale_notice(bot, raw_weather):
    await configured(bot)
    bot.clock.value = bot.clock.value.replace(hour=23, minute=40)
    original = deepcopy(raw_weather)
    change_weather(raw_weather, 8)
    await bot.poll_weather(DATASET, "新北市")
    raw_weather.clear()
    raw_weather.update(original)
    await bot.poll_weather(DATASET, "新北市")
    bot.clock.advance(8 * 3600)
    await bot.run_background()
    await bot.send_due()
    assert bot.line.pushes == []
    assert (await bot.weather.latest(CITIES["新北市"]))["periods"][8]["weather"] == original["records"]["Locations"][0]["Location"][0]["WeatherElement"][0]["Time"][8]["ElementValue"][0]["Weather"]


async def test_quota_defers_work_then_sends_when_available(bot, raw_weather):
    await configured(bot)
    bot.clock.advance(3 * 3600 + 600)
    bot.settings.monthly_limit = 0
    change_weather(raw_weather, 2)
    await bot.poll_weather(DATASET, "新北市")
    assert (await bot.db.one("SELECT status FROM jobs WHERE kind='weather_change'"))["status"] == "pending"
    assert await bot.db.all("SELECT id FROM outbox") == []
    bot.settings.monthly_limit = 200
    bot.clock.advance(61)
    await bot.run_background()
    await bot.send_due()
    assert len(bot.line.pushes) == 1


async def test_weather_model_failure_still_sends_factual_notice(bot, raw_weather):
    await configured(bot)
    bot.clock.advance(3 * 3600 + 600)
    bot.settings.chat_timeout = 0.01
    bot.model.delay = 1
    change_weather(raw_weather, 2)
    await bot.poll_weather(DATASET, "新北市")
    await bot.send_due()
    assert len(bot.line.pushes) == 1 and "陰天" in bot.line.pushes[0][1]["messages"][0]["text"]


async def test_concurrent_refresh_creates_one_change_event(bot, raw_weather):
    await configured(bot)
    bot.clock.advance(3 * 3600 + 600)
    change_weather(raw_weather, 2)
    await asyncio.gather(bot.weather.refresh(DATASET, "新北市"), bot.weather.refresh(DATASET, "新北市"))
    assert len(await bot.db.all("SELECT id FROM jobs WHERE kind='weather_change'")) == 1


async def test_snapshot_and_notice_work_rollback_together(bot, raw_weather, monkeypatch):
    await configured(bot)
    bot.clock.advance(3 * 3600 + 600)
    before = await bot.weather.latest(CITIES["新北市"])
    original = bot.db.conn.execute
    def fail_on_job(sql, parameters=()):
        if sql.startswith("INSERT INTO jobs"):
            raise RuntimeError("mock event persistence failure")
        return original(sql, parameters)
    change_weather(raw_weather, 2)
    with monkeypatch.context() as scoped:
        scoped.setattr(bot.db.conn, "execute", fail_on_job)
        with pytest.raises(RuntimeError):
            await bot.weather.refresh(DATASET, "新北市")
    assert (await bot.weather.latest(CITIES["新北市"]))["forecast_hash"] == before["forecast_hash"]
    assert await bot.db.all("SELECT id FROM jobs WHERE kind='weather_change'") == []
    await bot.poll_weather(DATASET, "新北市")
    await bot.send_due()
    assert len(bot.line.pushes) == 1


async def test_unsent_stale_notice_is_merged_before_delivery(bot, raw_weather):
    await configured(bot)
    bot.clock.advance(3 * 3600 + 600)
    change_weather(raw_weather, 2)
    await bot.poll_weather(DATASET, "新北市")
    change_weather(raw_weather, 2, "08", "短暫雨")
    await bot.weather.refresh(DATASET, "新北市")
    await bot.send_due()
    assert bot.line.pushes == []
    await bot.run_background()
    await bot.send_due()
    assert len(bot.line.pushes) == 1
    assert "短暫雨" in bot.line.pushes[0][1]["messages"][0]["text"]
    assert (await bot.db.one("SELECT status,error FROM outbox ORDER BY id LIMIT 1")) == {"status": "cancelled", "error": "superseded"}


async def test_remerging_keeps_earliest_baseline(bot, raw_weather):
    await configured(bot)
    bot.clock.advance(3 * 3600 + 600)
    for period in (2, 3):
        change_weather(raw_weather, period)
        await bot.weather.refresh(DATASET, "新北市")
    await bot.prepare_weather_changes()
    change_weather(raw_weather, 4)
    await bot.weather.refresh(DATASET, "新北市")
    await bot.send_due()
    await bot.run_background()
    await bot.send_due()
    metadata = json.loads((await bot.db.one("SELECT weather FROM outbox WHERE status='sent'"))["weather"])
    assert len(metadata["notified"]) == 3 and len(bot.line.pushes) == 1


async def test_late_notice_rebuilds_only_expired_periods_without_losing_future_changes(bot, raw_weather):
    await configured(bot)
    bot.clock.advance(3 * 3600 + 600)
    for period in (1, 2):
        change_weather(raw_weather, period)
    await bot.poll_weather(DATASET, "新北市")
    bot.clock.advance(2 * 3600 + 60)
    await bot.send_due()
    assert bot.line.pushes == []
    await bot.run_background()
    await bot.send_due()
    metadata = json.loads((await bot.db.one("SELECT weather FROM outbox WHERE status='sent'"))["weather"])
    assert len(metadata["notified"]) == 1 and len(bot.line.pushes) == 1
    assert metadata["notified"][0]["valid_from"].endswith("12:00:00+08:00")
