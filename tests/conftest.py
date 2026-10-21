"""隔離時鐘、HTTP、模型與 LINE token；保留實際資料庫及服務流程。"""

import asyncio
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timedelta
import json
from pathlib import Path
import subprocess
import sqlite3
import time

import httpx
import pytest
import pytest_asyncio

from weather_bot.ai import PROMPT_VERSION
from weather_bot.config import PROJECT_ROOT, Settings, TZ
from weather_bot.workers import Bot
from weather_bot.regions import CITIES, DATASET


class TestClock:
    __test__ = False

    def __init__(self):
        self.value = datetime(2026, 10, 21, 6, 50, tzinfo=TZ)

    def now(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)

    def monotonic(self):
        return time.monotonic()


class Model:
    def __init__(self):
        self.calls = []
        self.delay = 0
        self.memory_delay = 0
        self.bad_memory = False

    async def generate(self, system, data, *, memory=False):
        self.calls.append({"data": deepcopy(data), "memory": memory})
        await asyncio.sleep(self.memory_delay if memory else self.delay)
        if data.get("task") == "onboarding":
            return {"updates": []}
        if memory:
            if self.bad_memory:
                return {"summary": {"invalid": True}}
            updates = []
            old = {m["key"]: m for m in data["existing_memories"]}
            for msg in data["messages"]:
                if msg["role"] != "user":
                    continue
                if "海鹽布丁" in msg["content"]:
                    updates.append({"scope": "real_user", "category": "preferences", "key": "persona.address_user_as", "value": "海鹽布丁", "source_message_ids": [msg["id"]], "expected_version": old.get("persona.address_user_as", {}).get("version", 0)})
                if "溫柔一點" in msg["content"]:
                    updates.append({"category": "preferences", "key": "persona.style", "value": "溫柔", "source_message_ids": [msg["id"]], "expected_version": old.get("persona.style", {}).get("version", 0)})
            text = " ".join(m["content"] for m in data["messages"]) + json.dumps(data["summary"], ensure_ascii=False)
            return {"updates": updates, "forget_keys": [], "summary": {
                "current_topic": "明天出門安排" if "出門" in text else "日常聊天",
                "open_threads": ["還沒決定幾點出門"] if "還沒決定幾點出門" in text else [],
                "recent_context": "使用者喜歡海鹽布丁這個稱呼" if "海鹽布丁" in text else "日常生活",
                "story_state": "陪伴聊天"} if data["compact"] else None}
        facts = data.get("weather_facts", [])
        address = data.get("persona", {}).get("address_user_as", "")
        sentences = [f"{fact}。" for fact in facts] if facts else [f"{address}，今天辛苦啦。" if address else "今天辛苦啦。", "要不要再陪我聊一下呀？"]
        return {"sentences": sentences, "choices": [
            {"id": "1", "label": "聊聊今天", "user_text": "陪我聊聊今天的安排"},
            {"id": "2", "label": "再陪一下", "user_text": "先陪我輕鬆聊一下"},
            {"id": "3", "label": "看看天氣", "user_text": "查詢我所在地的天氣預報"}]}


class LineMock:
    def __init__(self, clock):
        self.clock, self.tokens, self.used = clock, {}, set()
        self.replies, self.pushes = [], []
        self.push_failures = 0

    def issue(self, token):
        self.tokens[token] = (self.clock.now().timestamp(), time.monotonic())

    async def reply(self, token, payload):
        start, wall = self.tokens[token]
        assert token not in self.used, "token 已使用"
        elapsed = max(self.clock.now().timestamp() - start, time.monotonic() - wall)
        assert elapsed < 60, "token 已逾期"
        self.used.add(token)
        self.replies.append({"elapsed_seconds": elapsed, "payload": deepcopy(payload)})

    async def push(self, user_id, payload, retry_key):
        self.pushes.append((user_id, deepcopy(payload), retry_key))
        if self.push_failures:
            self.push_failures -= 1
            raise httpx.ReadTimeout("mock timeout")

    async def quota(self):
        return 200, 0


@pytest.fixture
def raw_weather():
    return json.loads((Path(__file__).parent / "fixtures/cwa_counties.json").read_text(encoding="utf-8"))


@pytest_asyncio.fixture
async def bot(ci_database_path, raw_weather, request):
    clock, model = TestClock(), Model()
    line = LineMock(clock)
    async def handler(request):
        assert request.url.host == "opendata.cwa.gov.tw", "一般 CI 禁止真實網路"
        assert request.url.path.endswith("/" + DATASET)
        city = request.url.params["LocationName"]
        assert city in CITIES
        raw = deepcopy(raw_weather)
        location = raw["records"]["Locations"][0]["Location"][0]
        location.update(LocationName=city, Geocode=CITIES[city])
        return httpx.Response(200, json=raw)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        runtime = Bot(Settings(database=ci_database_path, line_secret="test-secret", cwa_key="fake-key"), clock=clock, http=http, ai=model, sender=line)
        await runtime.start(workers=False)
        runtime.model, runtime.line = model, line
        yield runtime
        result = next((r for r in reversed(RESULTS) if r["case_id"] == request.node.nodeid), None)
        if result is not None:
            result["evidence"] = {
                "database_path": Path(runtime.db.path).relative_to(PROJECT_ROOT).as_posix(),
                "reply_count": len(line.replies), "max_reply_seconds": max((r["elapsed_seconds"] for r in line.replies), default=None),
                "reply_under_60_seconds": all(r["elapsed_seconds"] < 60 for r in line.replies),
                "messages_per_reply": [len(r["payload"]["messages"]) for r in line.replies],
                "model_calls": len(runtime.model.calls),
                "reply_payloads": [r["payload"] for r in line.replies],
                "memory_rows": await runtime.db.all("SELECT id,key,category,scope,source_message_ids,version,forgotten FROM memories"),
                "jobs": await runtime.db.all("SELECT id,kind,status,attempts,error FROM jobs"),
                "outbox": await runtime.db.all("SELECT id,kind,status,memory_ids,attempts FROM outbox"),
            }
        await runtime.stop()


@pytest.fixture
def ci_database_path(request):
    """固定保留每案例最新資料庫；只覆寫帶本 fixture 標記的 CI 產物。"""
    directory = (PROJECT_ROOT / "logs" / "ci" / "databases").resolve()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{request.node.name}.sqlite3"
    if path.resolve().parent != directory:
        raise ValueError("CI 資料庫必須位於 logs/ci/databases")
    marker = 0x43495453
    if path.exists():
        with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as conn:
            if conn.execute("PRAGMA application_id").fetchone()[0] != marker:
                raise ValueError("拒絕覆寫沒有 CI 標記的資料庫")
        path.unlink()
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(f"PRAGMA application_id={marker}")
    return str(path)


async def configured(bot, uid="Utest", city="新北市"):
    await bot.db.ensure_user(uid)
    data, _ = await bot.weather.refresh(DATASET, city)
    await bot.db.execute("UPDATE users SET city=?,dataset=?,location_code=?,setup_step='done' WHERE id=?", (city, DATASET, data["location_code"], uid))
    return await bot.db.one("SELECT * FROM users WHERE id=?", (uid,))


async def chat(bot, text, event_id=None, uid="Utest", postback=None):
    event_id = event_id or f"e{len(bot.line.tokens)}"
    token = f"token-{event_id}"
    bot.line.issue(token)
    event = {"type": "postback" if postback else "message", "webhookEventId": event_id,
             "replyToken": token, "timestamp": int(bot.clock.now().timestamp() * 1000),
             "source": {"type": "user", "userId": uid}, "message": {"type": "text", "text": text}}
    if postback:
        event["postback"] = {"data": postback}
    await bot.receive(event)
    await bot.drain_chats()
    return event


def pytest_addoption(parser):
    parser.addoption("--live", action="store_true", help="執行真實服務驗證，LINE 仍捕捉 payload")


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--live"):
        for item in items:
            if "live" in item.keywords:
                item.add_marker(pytest.mark.skip(reason="NOT_RUN: 需 --live 明確啟用"))


RESULTS = []


def pytest_runtest_logreport(report):
    if report.when == "call" or report.when == "setup" and report.outcome in {"failed", "skipped"}:
        RESULTS.append({"case_id": report.nodeid, "status": {"passed": "PASS", "failed": "FAIL", "skipped": "NOT_RUN"}[report.outcome],
                        "duration_seconds": round(report.duration, 6), "properties": dict(report.user_properties), "reason": "" if report.passed else str(report.longrepr)[-1500:]})


def pytest_sessionfinish(session, exitstatus):
    path = Path("logs/ci/latest.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    commit = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True)
    acceptance = {}
    for case in ("T01", "T02", "T03", "T04", "T05", "T06", "T07"):
        matches = [r["status"] for r in RESULTS if f"test_{case}_" in r["case_id"]]
        acceptance[case] = "FAIL" if "FAIL" in matches else "PASS" if "PASS" in matches else "NOT_RUN"
    path.write_text(json.dumps({"commit": commit.stdout.strip() if commit.returncode == 0 else None,
        "model": "deterministic-fake (live cases use configured BIRC)", "prompt_version": PROMPT_VERSION,
        "exit_status": int(exitstatus), "acceptance": acceptance, "results": RESULTS,
        "line_delivery": "MOCK_ONLY", "reply_deadline_seconds": 60,
        "background_memory_excluded": True}, ensure_ascii=False, indent=2), encoding="utf-8")
