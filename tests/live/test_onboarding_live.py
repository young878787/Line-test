"""最多三次真實 BIRC 請求；天氣固定、LINE mock，正式庫不參與。"""

from copy import deepcopy
import json
from pathlib import Path
import time

import httpx
import pytest

from conftest import chat
from weather_bot.ai import AIClient, PROMPT_VERSION
from weather_bot.config import Settings

pytestmark = pytest.mark.live


async def test_T07_real_ai_onboarding_confirmation_memory_and_chat(bot):
    settings = Settings.from_env()
    target = Path("logs/live/onboarding.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    report = {"case_id": "T07_ONBOARDING", "status": "NOT_RUN", "model": settings.ai_model,
              "prompt_version": PROMPT_VERSION, "line_delivery": "MOCK_ONLY", "weather": "FIXTURE",
              "steps": [], "calls": []}
    if not settings.ai_key:
        target.write_text(json.dumps(report, indent=2), encoding="utf-8")
        pytest.skip("NOT_RUN: missing AI_API_Keys")

    class Model:
        calls = report["calls"]

        async def generate(self, system, data, *, memory=False):
            if len(self.calls) >= 3:
                raise ValueError("onboarding live call budget exhausted")
            entry = {"task": data.get("task", "chat"), "input": deepcopy(data)}
            self.calls.append(entry)
            started = time.monotonic()
            try:
                result = await client.generate(system, data, memory=memory)
                entry["output"] = result
                return result
            except Exception as error:
                entry["error_type"] = type(error).__name__
                raise
            finally:
                entry["seconds"] = time.monotonic() - started

    async def send(text):
        await chat(bot, text)
        user = await bot.db.one("SELECT * FROM users")
        job = await bot.db.one("SELECT error FROM jobs ORDER BY id DESC LIMIT 1")
        reply = bot.line.replies[-1]
        report["steps"].append({"input": text, "setup_step": user["setup_step"], "reply": reply["payload"]["messages"][0]["text"],
                                "reply_seconds": reply["elapsed_seconds"], "job_error": job["error"]})
        assert job["error"] is None
        return user

    async with httpx.AsyncClient() as http:
        client = AIClient(settings, http)
        bot.ai = bot.chat.ai = bot.model = Model()
        report["status"] = "FAIL"
        try:
            user = await send("我住新北市，早上7.30起床，請叫我海鹽布丁，口吻溫柔一點。")
            draft = json.loads(user["setup_data"])
            assert user["setup_step"] == "morning" and draft["city"]["value"] == "新北市" and draft["wake_time"]["value"] == "07:30"
            assert "district" not in draft
            for text in ("全部關閉", "保持關閉", "我想先聊天"):
                user = await send(text)
            assert user["setup_step"] == "confirm" and len(report["calls"]) == 1
            assert await bot.db.all("SELECT * FROM memories") == []
            user = await send("確認")
            assert user["setup_step"] == "done" and user["wake_time"] == "07:30" and user["city"] == "新北市" and user["district"] is None
            memories = await bot.db.all("SELECT * FROM memories")
            assert {m["key"]: m["value"] for m in memories} == {"persona.address_user_as": "海鹽布丁", "persona.style": "溫柔"}
            await send("你會怎麼稱呼我？")
            assert "海鹽布丁" in report["steps"][-1]["reply"]
            context = report["calls"][-1]["input"]
            assert context["persona"]["style"] == "溫柔" and context["user_settings"]["wake_time"] == "07:30"
            assert len(report["calls"]) == 2 and all(s["reply_seconds"] < 60 for s in report["steps"])
            assert bot.line.pushes == []
            report.update(status="PASS", settings={k: user[k] for k in ("city", "wake_time", "morning_enabled", "alerts_enabled", "night_forecast_enabled")},
                          memories=[{k: m[k] for k in ("key", "value", "source_message_ids")} for m in memories])
        except Exception as error:
            report["error_type"] = type(error).__name__
            raise
        finally:
            target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            lines = ["# 新手引導 AI 驗證", "", f"結果：{report['status']}；模型：{settings.ai_model}；LINE：mock。", ""]
            for step in report["steps"]:
                lines += [f"使用者：{step['input']}", f"階段：{step['setup_step']}；Reply：{step['reply_seconds']:.3f} 秒", "", step["reply"], ""]
            target.with_suffix(".md").write_text("\n".join(lines), encoding="utf-8")
