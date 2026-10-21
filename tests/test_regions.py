"""全臺縣市設定、預報地點、舊資料銜接與空值守門。"""

import json

import pytest

from conftest import chat, configured
from weather_bot.chat import weather_facts
from weather_bot.onboarding import setup_step
from weather_bot.regions import CITIES, DATASET, city_from_text, normalize_city


@pytest.mark.parametrize("city", CITIES, ids=list(CITIES.values()))
async def test_all_counties_complete_setup_without_district(bot, city):
    await chat(bot, city)
    user = await bot.db.one("SELECT * FROM users")
    assert user["setup_step"] == "wake"
    assert json.loads(user["setup_data"])["city"]["value"] == city
    for text in ("07:30", "開啟晨報", "起床後再提醒", "先跳過"):
        await chat(bot, text)
    assert (await bot.db.one("SELECT setup_step FROM users"))["setup_step"] == "confirm"
    await chat(bot, "確認開始")
    user = await bot.db.one("SELECT * FROM users")
    assert (user["city"], user["dataset"], user["location_code"], user["setup_step"]) == (city, DATASET, CITIES[city], "done")
    assert user["district"] is None
    text = bot.line.replies[-1]["payload"]["messages"][0]["text"]
    assert city in text and "None" not in text and "補行政區" not in text
    weather = await bot.weather.get_weather(user)
    assert weather["city"] == city and weather["location_code"] == CITIES[city]


@pytest.mark.parametrize("value", [None, "", "嘉義", "新竹", "北部", "板橋區", "臺中縣"])
def test_missing_or_ambiguous_city_cannot_finish_setup(value):
    draft = {"city": {"value": value}, "wake_time": {"value": "07:30"}, "morning_enabled": {"value": True},
             "night_forecast_enabled": {"value": False}, "_preferences_answered": True}
    assert setup_step(draft) == "region"
    assert normalize_city(value) is None


@pytest.mark.parametrize("text,city", [("台中", "臺中市"), ("台東縣", "臺東縣"), ("澎湖", "澎湖縣"), ("新北市板橋區", "新北市")])
def test_city_names_and_aliases(text, city):
    assert city_from_text(text) == city


@pytest.mark.parametrize("text", ["嘉義明天天氣", "新竹下雨嗎", "臺北市與高雄市天氣"])
def test_ambiguous_query_requires_one_county(text):
    with pytest.raises(ValueError):
        city_from_text(text)


async def test_county_setup_does_not_require_available_forecast(bot, monkeypatch):
    async def unavailable(*args):
        raise AssertionError("設定已知縣市不應呼叫預報 API")
    monkeypatch.setattr(bot.weather, "refresh", unavailable)
    await chat(bot, "臺中市")
    assert (await bot.db.one("SELECT setup_step FROM users"))["setup_step"] == "wake"


async def test_explicit_query_county_does_not_change_saved_county(bot):
    await configured(bot, city="臺北市")
    await chat(bot, "臺中市今天天氣如何？")
    text = bot.line.replies[-1]["payload"]["messages"][0]["text"]
    assert "臺中市" in text and "臺北市" not in text and "None" not in text
    assert (await bot.db.one("SELECT city FROM users"))["city"] == "臺北市"
    await chat(bot, "嘉義今天天氣如何？")
    text = bot.line.replies[-1]["payload"]["messages"][0]["text"]
    assert "嘉義市" in text and "嘉義縣" in text


async def test_county_migration_preserves_history_and_cancels_legacy_weather_job(bot):
    user = await configured(bot)
    await bot.db.execute("UPDATE users SET dataset='F-D0047-069',district='板橋區',location_code='65000010' WHERE id=?", (user["id"],))
    await bot.db.execute("INSERT INTO users(id,setup_step) VALUES ('invalid','done')")
    await bot.db.job("weather", None, "legacy-weather", {"dataset": "F-D0047-069", "district": "板橋區"}, bot.clock.now().timestamp())
    await bot.db.job("weather", None, "legacy-running-weather", {"dataset": "F-D0047-069", "district": "板橋區"}, bot.clock.now().timestamp())
    await bot.db.execute("UPDATE jobs SET status='running' WHERE business_key='legacy-running-weather'")
    before = await bot.db.all("SELECT * FROM weather_snapshots")
    await bot.db.close()
    await bot.db.open()
    migrated = await bot.db.one("SELECT * FROM users WHERE id=?", (user["id"],))
    assert (migrated["city"], migrated["dataset"], migrated["location_code"]) == ("新北市", DATASET, CITIES["新北市"])
    assert migrated["district"] == "板橋區" and migrated["setup_step"] == "done"
    assert await bot.db.all("SELECT * FROM weather_snapshots") == before
    assert (await bot.db.one("SELECT status FROM jobs WHERE business_key='legacy-weather'"))["status"] == "cancelled"
    assert (await bot.db.one("SELECT status FROM jobs WHERE business_key='legacy-running-weather'"))["status"] == "cancelled"
    assert (await bot.db.one("SELECT setup_step FROM users WHERE id='invalid'"))["setup_step"] == "region"
    await bot.tick()
    jobs = await bot.db.all("SELECT payload FROM jobs WHERE kind='weather' AND status='pending'")
    assert len(jobs) == 1 and json.loads(jobs[0]["payload"])["city"] == "新北市"


@pytest.mark.parametrize("mode", ["chat", "morning_brief", "weather_alert"])
async def test_missing_forecast_still_names_required_county(bot, mode):
    user = await configured(bot)
    assert user["district"] is None
    facts = weather_facts(user, None, mode, bot.clock.now())
    assert "新北市" in facts[0] and "None" not in "".join(facts)
