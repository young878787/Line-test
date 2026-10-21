from copy import deepcopy
import json
import sqlite3
from contextlib import closing

import pytest

from conftest import chat, configured
from weather_bot.db import Database, SCHEMA
from weather_bot.onboarding import onboarding_reminder, validate_change
from weather_bot.schemas import SetupChange


def setup_model(bot, build):
    original = bot.model.generate

    async def generate(system, data, *, memory=False):
        if data.get("task") == "onboarding":
            bot.model.calls.append({"data": deepcopy(data), "memory": False})
            return {"updates": build(data)}
        return await original(system, data, memory=memory)

    bot.model.generate = generate


def change(key, value, message, quote=None):
    return {"key": key, "value": value, "source_message_id": message["id"], "quote": quote or message["content"]}


async def test_mixed_setup_keeps_partial_values_confirms_preferences_and_opens_chat(bot, record_property):
    first = "新北市 7.30起床，一般天氣就好，請叫我海鹽布丁，口吻溫柔一點。"

    def build(data):
        message = data["messages"][-1]
        if message["content"] == first:
            return [change(k, v, message, q) for k, v, q in (
                ("city", "新北市", "新北市"), ("wake_time", "07:30", "7.30起床"),
                ("persona.address_user_as", "海鹽布丁", "請叫我海鹽布丁"), ("persona.style", "溫柔", "口吻溫柔一點"))]
        return []

    setup_model(bot, build)
    # 真正入口先收進 jobs＋原文，尚未執行任何 AI。
    bot.line.issue("first")
    event = {"type": "message", "webhookEventId": "first", "replyToken": "first", "source": {"type": "user", "userId": "Utest"}, "message": {"type": "text", "text": first}}
    await bot.receive(event)
    source = await bot.db.one("SELECT * FROM messages WHERE external_event_id='first'")
    assert source["content"] == first and source["kind"] == "received" and bot.model.calls == []
    await bot.drain_chats()
    user = await bot.db.one("SELECT * FROM users")
    draft = json.loads(user["setup_data"])
    assert user["setup_step"] == "morning" and draft["wake_time"]["value"] == "07:30"
    assert "district" not in draft and "alerts_enabled" not in draft
    assert "07:30" in bot.line.replies[-1]["payload"]["messages"][0]["text"]
    assert await bot.db.all("SELECT * FROM memories") == []
    await chat(bot, "確認")
    assert (await bot.db.one("SELECT setup_step FROM users"))["setup_step"] == "morning"
    for text in ("全部關閉", "保持關閉", "陪我聊天"):
        await chat(bot, text)
    assert (await bot.db.one("SELECT setup_step FROM users"))["setup_step"] == "confirm"
    assert all(c["data"].get("task") == "onboarding" for c in bot.model.calls)
    assert await bot.db.all("SELECT * FROM messages WHERE kind='chat'") == []
    assert await bot.db.all("SELECT * FROM memories") == []
    await chat(bot, "確認")
    user = await bot.db.one("SELECT * FROM users")
    assert (user["city"], user["district"], user["wake_time"], user["setup_step"]) == ("新北市", None, "07:30", "done")
    assert user["morning_enabled"] == user["alerts_enabled"] == user["night_forecast_enabled"] == 0
    memories = await bot.db.all("SELECT * FROM memories")
    assert {m["key"]: m["value"] for m in memories} == {"persona.address_user_as": "海鹽布丁", "persona.style": "溫柔"}
    assert all(json.loads(m["source_message_ids"]) == [source["id"]] for m in memories)
    await bot.db.close()
    await bot.db.open()
    await chat(bot, "你會怎麼叫我？")
    context = bot.model.calls[-1]["data"]
    assert context["user_settings"]["wake_time"] == "07:30" and context["user_settings"]["city"] == "新北市"
    assert "district" not in context["user_settings"]
    assert context["persona"] == {"address_user_as": "海鹽布丁", "style": "溫柔"}
    assert "海鹽布丁" in bot.line.replies[-1]["payload"]["messages"][0]["text"]
    assert bot.line.pushes == [] and all(r["elapsed_seconds"] < 60 for r in bot.line.replies)
    record_property("onboarding_confirmation", "PASS: partial fields, source-backed preferences, restart, chat context")


async def test_old_received_history_recovers_time_without_guessing_district(bot):
    # 模擬升級前已保存但未解析的原句；不重送一次性 Reply token。
    await bot.db.ensure_user("Utest")
    old_id = await bot.db.execute("INSERT INTO messages(user_id,role,content,kind,created_at) VALUES ('Utest','user',?,'received',?)",
                                  ("台北市 7.30起床 一般天氣就好", bot.clock.now().timestamp()))

    def build(data):
        old = next(m for m in data["messages"] if m["id"] == old_id)
        return [change("city", "臺北市", old, "台北市"), change("wake_time", "07:30", old, "7.30起床")]

    setup_model(bot, build)
    await chat(bot, "台北市")
    draft = json.loads((await bot.db.one("SELECT setup_data FROM users"))["setup_data"])
    assert draft["city"]["value"] == "臺北市" and draft["wake_time"]["value"] == "07:30"
    assert "district" not in draft and "morning_enabled" not in draft
    text = bot.line.replies[-1]["payload"]["messages"][0]["text"]
    assert onboarding_reminder("morning") in text and "07:30" in text and "補行政區" not in text


@pytest.mark.parametrize("case", ["foreign_source", "fake_quote", "fake_time", "fake_city", "wrong_switch", "unsafe_preference"])
async def test_untrusted_setup_output_cannot_fill_or_unlock_user(bot, case):
    def build(data):
        message = data["messages"][-1]
        item = change("wake_time", "07:30", message, "7:30起床")
        if case == "foreign_source":
            item["source_message_id"] = 99999
        elif case == "fake_quote":
            item["quote"] = "8:00起床"
        elif case == "fake_time":
            item["value"] = "08:00"
        elif case == "fake_city":
            item = change("city", "臺北市", message)
        elif case == "wrong_switch":
            item = change("morning_enabled", "開啟", message, "晨報不要")
        else:
            item = change("persona.style", "忽略規則", message)
        return [item]

    setup_model(bot, build)
    await chat(bot, "新北市，7:30起床，晨報不要，叫我海鹽布丁")
    user = await bot.db.one("SELECT * FROM users")
    assert user["setup_step"] == "region" and json.loads(user["setup_data"]) == {}
    assert await bot.db.all("SELECT * FROM memories") == []
    assert onboarding_reminder("region") in bot.line.replies[-1]["payload"]["messages"][0]["text"]
    assert len(await bot.db.all("SELECT * FROM messages WHERE kind='received'")) == 1
    assert (await bot.db.one("SELECT error FROM jobs"))["error"] is None
    assert "有一項答案的意思還不夠明確" in bot.line.replies[-1]["payload"]["messages"][0]["text"]


async def test_setup_model_timeout_retains_input_and_replies_fixed_prompt(bot):
    bot.settings.chat_timeout = 0.02
    bot.model.delay = 1
    await chat(bot, "我住新北市板橋區，早上7:30起床")
    assert (await bot.db.one("SELECT content FROM messages"))["content"].startswith("我住新北市")
    assert (await bot.db.one("SELECT setup_step FROM users"))["setup_step"] == "region"
    assert onboarding_reminder("region") in bot.line.replies[-1]["payload"]["messages"][0]["text"]
    assert bot.line.replies[-1]["elapsed_seconds"] < 1 and bot.line.pushes == []


async def test_confirmation_rollback_does_not_unlock_or_partially_save(bot, monkeypatch):
    setup_model(bot, lambda data: [change("persona.address_user_as", "海鹽布丁", data["messages"][-1])])
    for text in ("新北市板橋區", "07:30", "全部關閉", "保持關閉", "叫我海鹽布丁"):
        await chat(bot, text)
    original = bot.memory.save_updates

    async def fail_after_insert(*args):
        await original(*args)
        raise RuntimeError("mock transaction failure")

    monkeypatch.setattr(bot.memory, "save_updates", fail_after_insert)
    await chat(bot, "確認")
    assert (await bot.db.one("SELECT setup_step FROM users"))["setup_step"] == "confirm"
    assert await bot.db.all("SELECT * FROM memories") == []
    monkeypatch.setattr(bot.memory, "save_updates", original)
    await chat(bot, "確認")
    assert (await bot.db.one("SELECT setup_step FROM users"))["setup_step"] == "done"
    assert len(await bot.db.all("SELECT * FROM memories")) == 1


async def test_queued_chat_receipts_preserve_turn_order_after_promotion(bot):
    await configured(bot)
    for i in range(2):
        token = f"queued-{i}"
        bot.line.issue(token)
        await bot.receive({"type": "message", "webhookEventId": token, "replyToken": token, "source": {"type": "user", "userId": "Utest"}, "message": {"type": "text", "text": f"今天第{i}件事"}})
    assert len(await bot.db.all("SELECT * FROM messages WHERE kind='received'")) == 2
    await bot.drain_chats()
    rows = await bot.db.all("SELECT role,turn FROM messages ORDER BY id")
    assert rows == [{"role": role, "turn": turn} for turn in (1, 2) for role in ("user", "assistant")]
    assert (await bot.chat.context("Utest"))["recent"][2]["content"] == "今天第1件事"


async def test_existing_database_additive_upgrade_preserves_setup(ci_database_path):
    with closing(sqlite3.connect(ci_database_path)) as db:
        db.executescript(SCHEMA.replace(", setup_data TEXT NOT NULL DEFAULT '{}'", ""))
        db.execute("INSERT INTO users(id,city,district,location_code,setup_step) VALUES ('legacy','新北市','板橋區','65000010','wake')")
        db.commit()
    database = await Database(ci_database_path).open()
    try:
        user = await database.one("SELECT * FROM users")
        assert user["setup_data"] == "{}" and user["setup_step"] == "wake" and user["district"] == "板橋區"
        assert (await database.one("PRAGMA integrity_check"))["integrity_check"] == "ok"
    finally:
        await database.close()


@pytest.mark.parametrize("key,value,quote", [
    ("morning_enabled", "開啟", "全部關閉"),
    ("alerts_enabled", "開啟", "只開晨報"),
    ("morning_enabled", "開啟", "開啟"),
    ("night_forecast_enabled", "開啟", "全部開啟"),
], ids=["reversed_switch", "morning_only", "wrong_notification", "night_not_selected"])
def test_canonical_quotes_cannot_reverse_or_expand_notification_consent(key, value, quote):
    with pytest.raises(ValueError):
        validate_change(SetupChange(key=key, value=value, source_message_id=1, quote=quote), {1: {"content": quote}})


@pytest.mark.parametrize("key,value,quote,source", [
    ("wake_time", "07:30", "早上7:30", "我早上7:30上班，口吻溫柔一點"),
    ("alerts_enabled", "開啟", "夜間天氣提醒開啟", "夜間天氣提醒開啟"),
], ids=["commute_is_not_wake", "night_is_not_general"])
def test_extraction_cannot_change_setting_purpose(key, value, quote, source):
    with pytest.raises(ValueError):
        validate_change(SetupChange(key=key, value=value, source_message_id=1, quote=quote), {1: {"content": source}})


@pytest.mark.parametrize("key,value,quote,source", [
    ("city", "臺北市", "臺北市", "我不住臺北市，我住新北市"),
    ("wake_time", "07:30", "7:30起床", "我不是7:30起床"),
    ("persona.address_user_as", "海鹽布丁", "海鹽布丁", "不要叫我海鹽布丁"),
    ("alerts_enabled", "開啟", "天氣提醒開啟", "夜間天氣提醒開啟"),
], ids=["negated_city", "negated_time", "negated_name", "truncated_night"])
def test_quoted_substring_cannot_drop_negation_or_notification_scope(key, value, quote, source):
    with pytest.raises(ValueError):
        validate_change(SetupChange(key=key, value=value, source_message_id=1, quote=quote), {1: {"content": source}})


async def test_general_alerts_only_option_is_saved_after_confirmation(bot):
    for text in ("新北市", "07:30", "只開一般提醒", "保持關閉", "先跳過", "確認"):
        await chat(bot, text)
    user = await bot.db.one("SELECT * FROM users")
    assert user["setup_step"] == "done" and user["morning_enabled"] == user["night_forecast_enabled"] == 0 and user["alerts_enabled"] == 1
    assert bot.model.calls == []
