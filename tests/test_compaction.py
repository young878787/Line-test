import json
import pytest

from conftest import chat, configured


async def test_T05_19_20_boundary_15_plus_5_raw_40_and_retry(bot):
    await configured(bot)
    for i in range(19):
        await chat(bot, "朋友都叫我海鹽布丁，我也比較喜歡這個稱呼。" if i == 0 else "明天還沒決定幾點出門" if i == 10 else f"聊第{i+1}件事")
    assert await bot.db.all("SELECT * FROM jobs WHERE kind='compact'") == []
    event = await chat(bot, "今天聊得很開心")
    await bot.receive(event)
    await bot.drain_chats()
    assert len(await bot.db.all("SELECT * FROM jobs WHERE kind='compact'")) == 1
    assert len(await bot.db.all("SELECT * FROM messages")) == 40
    bot.model.bad_memory = True
    await bot.run_background()
    state = await bot.db.one("SELECT * FROM conversation_state")
    assert state["summary_through"] == 0 and state["memory_processed_until"] == 0
    bot.model.bad_memory = False
    bot.clock.advance(31)
    await bot.run_background()
    state = await bot.db.one("SELECT * FROM conversation_state")
    assert state["summary_through"] == 30
    compact = next(c["data"] for c in reversed(bot.model.calls) if c["memory"] and c["data"]["compact"])
    assert [m["id"] for m in compact["messages"]] == list(range(1, 31))
    assert json.loads(state["summary"])["open_threads"] == ["還沒決定幾點出門"]
    context = await bot.chat.context("Utest")
    assert [m["id"] for m in context["recent"]] == list(range(31, 41))
    assert context["memories"][0]["value"] == "海鹽布丁"
    assert len(await bot.db.all("SELECT * FROM messages")) == 40


async def test_compaction_transaction_rollback(bot):
    await configured(bot)
    for i in range(20):
        await chat(bot, "朋友都叫我海鹽布丁，我也比較喜歡這個稱呼。" if i == 0 else f"第{i}輪")
    await bot.db.execute("CREATE TRIGGER fail_summary BEFORE UPDATE OF summary ON conversation_state BEGIN SELECT RAISE(ABORT,'injected'); END")
    with pytest.raises(Exception):
        await bot.memory.organize("Utest", compact=True)
    assert await bot.db.all("SELECT * FROM memories") == []
    state = await bot.db.one("SELECT * FROM conversation_state")
    assert state["summary_through"] == state["memory_processed_until"] == 0
