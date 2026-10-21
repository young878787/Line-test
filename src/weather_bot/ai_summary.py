"""
vLLM 天氣摘要生成模組，使用 OpenAI 相容的 Chat Completions API。

重要規則：
- 只使用繁體中文
- 嚴禁幻覺：只能基於實際傳入的天氣數據生成內容
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import requests
from dotenv import load_dotenv

logger = logging.getLogger(__name__)


class VLLMClient:
    """vLLM API 客戶端（天氣摘要專用，關閉 thinking）。"""

    def __init__(
        self,
        api_key: str,
        url: str,
        model: str,
        timeout: float = 180.0,
        max_tokens: int = 4096,
    ) -> None:
        self.api_key = api_key
        base_url = url.strip().rstrip("/")
        if base_url.endswith("/chat/completions"):
            self.url = base_url
        elif base_url.endswith("/v1"):
            self.url = f"{base_url}/chat/completions"
        else:
            self.url = f"{base_url}/v1/chat/completions"
        self.model = model
        self.timeout = timeout
        self.max_tokens = max_tokens

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int = 2048,
    ) -> str:
        """
        傳送對話訊息，回傳 AI 回覆的純文字內容。

        Args:
            messages: OpenAI 格式的對話訊息列表
            max_tokens: 回覆最大 token 數

        Returns:
            AI 回覆的純文字
        """
        logger.info("正在呼叫 vLLM AI（模型：%s）…", self.model)

        response = requests.post(
            self.url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "weather-bot/1.0",
            },
            json={
                "model": self.model,
                "messages": messages,
                "temperature": 0.3,
                "max_tokens": min(self.max_tokens, max_tokens),
                "chat_template_kwargs": {"enable_thinking": False},
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        data = response.json()

        # 只取正式回覆 content，不將 reasoning_content 當作摘要。
        try:
            choice = data["choices"][0]
            message = choice["message"]
            content = message.get("content", "")
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError("vLLM API 回傳格式異常") from exc

        # 處理 content 可能是 list 的情況（部分模型回傳）
        if isinstance(content, list):
            text_parts = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    text_parts.append(part.get("text", ""))
                elif isinstance(part, str):
                    text_parts.append(part)
            content = "\n".join(text_parts)

        if content is None:
            content = ""
        if not isinstance(content, str):
            raise ValueError("vLLM API 回傳內容不是文字")
        content = content.strip()
        if not content:
            raise ValueError("vLLM AI 回傳空內容")

        logger.info("AI 摘要生成完成（%d 字元）", len(content))
        return content


def create_client_from_env() -> VLLMClient:
    """從 .env 環境變數建立 vLLM 客戶端。"""
    load_dotenv()

    api_key = os.getenv("AI_API_Keys", "").strip()
    if not api_key:
        raise ValueError(
            "AI_API_Keys 未設定，請寫入 .env 檔案"
        )

    return VLLMClient(
        api_key=api_key,
        url=os.getenv(
            "AI_API_URL",
            "https://chatapi.ntubimdbirc.tw",
        ),
        model=os.getenv("AI_MODEL", "BIRC-LLM"),
        timeout=float(os.getenv("AI_TIMEOUT", "180")),
        max_tokens=int(os.getenv("AI_MAX_TOKENS", "4096")),
    )


# ──────────────────────────────────────────────
# 天氣摘要 System Prompt（繁體中文 + 反幻覺）
# ──────────────────────────────────────────────

WEATHER_SYSTEM_PROMPT = """\
你是台灣天氣早報播報員。請根據以下規則生成天氣摘要：

【嚴格規則】
1. 只能使用繁體中文。
2. 嚴禁幻覺：只能使用我提供的天氣數據，不得捏造、推測或補充任何未提供的資訊。
3. 不得虛構溫度、降雨機率、天氣現象等數值。
4. 若某項數據缺失，直接跳過該項目，不要編造。

【格式要求】
1. 像親切的早安問候，適合 LINE 訊息閱讀。
2. 加入適當的 emoji 讓訊息更生動（如 ☀️🌧️🌡️💨☂️👕）。
3. 包含以下資訊（若數據有提供）：
   - 今日天氣概況
   - 溫度範圍
   - 降雨機率
   - 舒適度
   - 穿著/攜帶雨具建議
4. 控制在 300 字以內，簡潔有力。
5. 回傳純文字，不要使用 Markdown 格式。
"""


def generate_weather_summary(weather_data: dict[str, Any]) -> str:
    """
    使用 vLLM AI 生成天氣早報摘要。

    Args:
        weather_data: 由 weather.py 取得的結構化天氣資料

    Returns:
        適合 LINE 推播的繁體中文天氣摘要文字
    """
    client = create_client_from_env()

    # 將天氣數據格式化為 AI 可讀的文字
    user_content = (
        "以下是中央氣象署提供的台北市天氣預報數據，"
        "請據此生成今日天氣早報：\n\n"
        f"{json.dumps(weather_data, ensure_ascii=False, indent=2)}"
    )

    messages = [
        {"role": "system", "content": WEATHER_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]

    summary = client.chat(messages, max_tokens=1024)
    return summary
