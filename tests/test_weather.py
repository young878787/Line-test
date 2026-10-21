from copy import deepcopy
from datetime import timedelta
import pytest

from conftest import configured
from weather_bot.weather import normalize, forecast_changes, is_night


async def test_T01_adapter_time_alignment_null_and_same_version_zero_ai(bot, raw_weather):
    data, changes = await bot.weather.refresh("F-D0047-089", "新北市")
    assert data["location_code"] == "65000000" and changes == []
    assert data["periods"][0]["temperature"] == 23
    assert data["periods"][0]["pop_3h"] == 20
    assert data["periods"][1]["pop_3h"] is None
    assert data["periods"][0]["valid_from"] == "2026-10-21T06:00:00+08:00"
    bot.clock.advance(3600)
    await bot.poll_weather("F-D0047-089", "新北市")
    assert bot.model.calls == []
    assert await bot.db.all("SELECT * FROM outbox") == []
    assert len(await bot.db.all("SELECT * FROM weather_snapshots")) == 1


def test_same_hash_fetched_noise_and_all_forecast_changes(raw_weather, bot):
    old = normalize(raw_weather, "新北市", "F-D0047-089", bot.clock.now())
    same = normalize(raw_weather, "新北市", "F-D0047-089", bot.clock.now() + timedelta(minutes=5))
    assert old["forecast_hash"] == same["forecast_hash"]
    new = deepcopy(old)
    new["periods"][0].update(weather_code=7, weather="陰天")
    assert len(forecast_changes(old, new, bot.clock.now())) == 1
    new["periods"][0].update(weather_code=8, rain_state="rain")
    assert len(forecast_changes(old, new, bot.clock.now())) == 1
    new["periods"][0]["valid_from"] = "2026-10-21T09:30:00+08:00"
    assert forecast_changes(old, new, bot.clock.now()) == []


def test_incomplete_and_expired_are_rejected(raw_weather, bot):
    raw_weather["records"]["Locations"][0]["Location"][0]["WeatherElement"] = []
    with pytest.raises(ValueError):
        normalize(raw_weather, "新北市", "F-D0047-089", bot.clock.now())


async def test_night_toggle_no_daily_limit_and_retry_same_payload(bot, raw_weather):
    user = await configured(bot)
    bot.clock.value = bot.clock.value.replace(hour=23, minute=40)
    assert is_night(bot.clock.now())
    assert not await bot.alert_allowed(user)
    await bot.db.execute("UPDATE users SET night_forecast_enabled=1 WHERE id=?", (user["id"],))
    user = await bot.db.one("SELECT * FROM users WHERE id=?", (user["id"],))
    assert await bot.alert_allowed(user)
    weather = await bot.weather.get_weather(user)
    await bot.prepare_push(user, "alert-test", "weather_alert", weather, bot.clock.now().timestamp(), bot.clock.now().timestamp()+1800)
    assert await bot.alert_allowed(user)
    calls = len(bot.model.calls)
    bot.line.push_failures = 1
    await bot.send_due()
    bot.clock.advance(31)
    await bot.send_due()
    assert bot.line.pushes[0] == bot.line.pushes[1]
    assert len(bot.model.calls) == calls


async def test_budget_reserves_morning_and_uninformed_unknown_no_alert(bot):
    user = await configured(bot)
    bot.settings.monthly_limit = 10
    bot.clock.value = bot.clock.value.replace(hour=10)
    assert not await bot.alert_allowed(user)


async def test_cloud_only_new_version_zero_ai_and_night_off_cache_only(bot, raw_weather):
    await configured(bot)
    element = raw_weather["records"]["Locations"][0]["Location"][0]["WeatherElement"][0]
    element["Time"][0]["ElementValue"][0].update(Weather="陰天", WeatherCode="07")
    await bot.poll_weather("F-D0047-089", "新北市")
    assert bot.model.calls == []
    bot.clock.value = bot.clock.value.replace(hour=23, minute=40)
    element["Time"][6]["ElementValue"][0].update(Weather="短暫雨", WeatherCode="08")
    await bot.poll_weather("F-D0047-089", "新北市")
    assert len(await bot.db.all("SELECT id FROM weather_snapshots")) == 3
    assert bot.model.calls == [] and await bot.db.all("SELECT id FROM outbox") == []
