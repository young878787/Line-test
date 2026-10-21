import httpx
import pytest

from chat_scenario import ScenarioAI, run_five_turns, write_chat_report
from conftest import configured
from weather_bot.ai import AIClient, PROMPT_VERSION
from weather_bot.config import Settings

pytestmark = pytest.mark.live


async def test_T06_real_ai_chat_five_turns(bot):
    settings = Settings.from_env()
    if not settings.ai_key:
        write_chat_report({"case_id": "T06_CHAT_FIVE_TURNS", "status": "NOT_RUN", "model": settings.ai_model,
                           "prompt_version": PROMPT_VERSION, "line_delivery": "MOCK_ONLY", "turns": [],
                           "reason": "缺少 AI_API_Keys，未執行真實五輪聊天。"}, "logs/live/chat_five_turns.json")
        pytest.skip("NOT_RUN: 缺少 AI_API_Keys")
    await configured(bot)
    async with httpx.AsyncClient() as http:
        report = await run_five_turns(bot, ScenarioAI(AIClient(settings, http)),
                                     output="logs/live/chat_five_turns.json", model_name=settings.ai_model)
    assert report["status"] == "PASS", "五輪 AI 驗證未通過，詳見 logs/live/chat_five_turns.json"
