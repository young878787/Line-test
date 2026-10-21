import json

import httpx
import pytest

from weather_bot.ai import AIClient, AIOutputError
from weather_bot.config import Settings
from weather_bot.schemas import Reply


async def test_async_model_contract_schema_deadline_thinking_and_no_reasoning_fallback():
    calls = []
    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"updates":[]}'}, "finish_reason": "stop"}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = AIClient(Settings(ai_key="fake"), http)
        assert await client.generate("固定指令", {"messages": [{"id": 1, "role": "user", "content": "使用者"}, {"id": 2, "role": "assistant", "content": "角色"}]}, memory=True) == {"updates": []}
    body = calls[0]
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["name"] == "memory_result"
    assert body["response_format"]["json_schema"]["schema"]["$defs"]["MemoryUpdate"]["properties"]["source_message_ids"]["items"]["enum"] == [1]
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert "固定指令" in body["messages"][1]["content"]


async def test_invalid_json_retains_raw_for_sanitized_live_report():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"choices": [{"message": {"content": "不是 JSON"}}]}))) as http:
        with pytest.raises(AIOutputError) as error:
            await AIClient(Settings(ai_key="fake"), http).generate("固定規則", {})
    assert error.value.raw_output == "不是 JSON"


def test_structured_schema_enforces_one_sentence_per_item():
    schema = Reply.model_json_schema()
    assert schema["properties"]["sentences"]["items"]["pattern"] == r"^[^。！？!?\r\n]+[。！？!?]$"
    with pytest.raises(ValueError):
        Reply.model_validate({"sentences": ["海鹽布丁？這個稱呼聽起來甜甜的。", "今天辛苦啦。"], "choices": [
            {"id": str(i), "label": str(i), "user_text": str(i)} for i in range(1, 4)]})


async def test_setup_model_schema_restricts_sources_and_uses_chat_deadline():
    calls = []

    def handler(request):
        calls.append((json.loads(request.content), request.extensions["timeout"]))
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"updates":[]}'}, "finish_reason": "stop"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await AIClient(Settings(ai_key="fake"), http).generate("固定設定規則", {"task": "onboarding", "messages": [{"id": 11, "content": "7:30起床"}]})
    body, timeout = calls[0]
    assert body["response_format"]["json_schema"]["name"] == "setup_result"
    assert body["response_format"]["json_schema"]["schema"]["$defs"]["SetupChange"]["properties"]["source_message_id"]["enum"] == [11]
    assert timeout["read"] == 25 and body["chat_template_kwargs"]["enable_thinking"] is False
