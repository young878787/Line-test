from __future__ import annotations

from copy import deepcopy
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

import requests
from dotenv import load_dotenv


class TranslationMemoryResponseError(ValueError):
    def __init__(self, message: str, *, diagnostics: dict[str, Any]):
        super().__init__(message)
        self.diagnostics = diagnostics

    def with_context(self, **extra: Any) -> TranslationMemoryResponseError:
        merged = deepcopy(self.diagnostics)
        merged.update(extra)
        return TranslationMemoryResponseError(str(self), diagnostics=merged)


@dataclass(frozen=True)
class TranslationMemoryClient:
    api_key: str
    url: str
    model: str
    thinking: str = "disabled"
    timeout: float = 180.0
    max_tokens: int = 4096
    temperature: float = 0.1
    _response_failures: list[dict[str, Any]] = field(default_factory=list, init=False, repr=False, compare=False)

    def record_response_failure(self, diagnostics: dict[str, Any]) -> None:
        self._response_failures.append(deepcopy(diagnostics))

    def drain_response_failures(self) -> list[dict[str, Any]]:
        failures = deepcopy(self._response_failures)
        self._response_failures.clear()
        return failures

    def chat_json(self, messages: list[dict[str, str]], *, max_tokens: int = 2048) -> dict[str, Any]:
        response = requests.post(
            self.url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.model,
                "messages": messages,
                "temperature": self.temperature,
                "max_tokens": max_tokens,
                "thinking": {"type": self.thinking},
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        data = response.json()
        try:
            choice = data["choices"][0]
            message = choice["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise TranslationMemoryResponseError(
                "OpenCode returned unexpected response shape",
                diagnostics={
                    "failure_kind": "unexpected_response_shape",
                    "model": self.model,
                    "url": self.url,
                    "thinking": self.thinking,
                    "requested_max_tokens": max_tokens,
                    "response_json": data,
                },
            ) from exc

        diagnostics = _build_response_diagnostics(
            data,
            message=message,
            finish_reason=choice.get("finish_reason"),
            content=_stringify_message_content(message.get("content") if isinstance(message, dict) else message),
            model=self.model,
            url=self.url,
            thinking=self.thinking,
            requested_max_tokens=max_tokens,
        )
        content = _select_content_before_parse(diagnostics)
        if not content.strip():
            raise TranslationMemoryResponseError(
                _format_response_error("OpenCode response content is empty", diagnostics),
                diagnostics={**diagnostics, "failure_kind": "empty_content"},
            )
        return parse_json_response(content, diagnostics=diagnostics)


def create_client_from_env() -> TranslationMemoryClient:
    load_dotenv()
    api_key = os.getenv("OPENCODE_GO_API_KEY")
    if not api_key:
        raise ValueError("OPENCODE_GO_API_KEY is required")
    return TranslationMemoryClient(
        api_key=api_key,
        url=os.getenv("OPENCODE_GO_CHAT_COMPLETIONS_URL", "https://opencode.ai/zen/go/v1/chat/completions"),
        model=os.getenv(
            "TRANSLATION_MODEL",
            os.getenv(
                "OPENCODE_TRANSLATION_MODEL",
                os.getenv("PUNCTUATION_MODEL", os.getenv("OPENCODE_GO_PUNCTUATION_MODEL", "deepseek-v4-flash")),
            ),
        ),
        thinking=os.getenv("TRANSLATION_THINKING", os.getenv("OPENCODE_GO_THINKING", "disabled")),
        timeout=float(os.getenv("TRANSLATION_TIMEOUT", os.getenv("OPENCODE_GO_TIMEOUT", "180"))),
        max_tokens=int(os.getenv("TRANSLATION_MAX_TOKENS", os.getenv("OPENCODE_GO_MAX_TOKENS", "4096"))),
        temperature=0.1,
    )


def parse_json_response(content: str, *, diagnostics: dict[str, Any] | None = None) -> dict[str, Any]:
    cleaned = content.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned).strip()
    match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if match:
        cleaned = match.group(0)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        payload = deepcopy(diagnostics) if diagnostics is not None else {}
        payload.update(
            {
                "failure_kind": "json_decode_error",
                "parse_candidate": cleaned,
                "json_error": str(exc),
            }
        )
        raise TranslationMemoryResponseError(
            _format_response_error("OpenCode JSON parse failed", payload),
            diagnostics=payload,
        ) from exc
    if not isinstance(data, dict):
        payload = deepcopy(diagnostics) if diagnostics is not None else {}
        payload.update(
            {
                "failure_kind": "non_object_json",
                "parse_candidate": cleaned,
                "parsed_type": type(data).__name__,
            }
        )
        raise TranslationMemoryResponseError(
            _format_response_error("OpenCode JSON response must be an object", payload),
            diagnostics=payload,
        )
    return data


def translate_segment(
    client: TranslationMemoryClient,
    segment: dict[str, Any],
    *,
    glossary_hits: list[dict[str, str]],
) -> dict[str, Any]:
    messages = [
        {
            "role": "system",
            "content": (
                "你是日文 VTuber 直播轉錄的繁體中文翻譯器。\n"
                "只做忠實翻譯，不摘要、不補充、不評論、不修正事實。\n"
                "優先採用 glossary 的固定譯名。只回傳 JSON object。\n"
                "\n"
                "【glossary 欄位處理】\n"
                "- glossary_hits[*].zh 欄位可能含有全形括號註釋，如「潤羽露西婭（VTuber 本名）」\n"
                "- 翻譯時只採用「括號前」的純譯名部分，**不要把括號內的註釋寫進 zh_tw_text**\n"
                "- 括號內的中文註釋是給人類閱讀的 metadata，不是翻譯內容的一部分\n"
                "\n"
                "【反幻覺規則（嚴格遵守，違反即 needs_review）】\n"
                "1. 不得補完原文未提及的人名：原文若只提到「うるはるしあ」就只譯為「潤羽るしあ」/"
                "「潤羽露西亞」，**不得自行推測並寫出原文沒有的其他 VTuber 人名（例如「一青空」、"
                "「○○醬」等）**\n"
                "2. 自我介紹／自我指涉段必須 1:1 對應：原文「うるはるしあ自我介紹」"
                "→「潤羽るしあ的自我介紹」；原文若只說名字就只譯名字，**不得替換成另一個真實存在的"
                " VTuber、實況主、人物**\n"
                "3. 不得為求譯文通順而修改身分、職業、關係、事件主詞\n"
                "4. 對於原文含糊、不可解、或顯然是語音辨識殘留的片段（如連續假名、單字符、無語意"
                " 字串），應如實保留或加註「（語音辨識不確定）」，**不得據此推測具體人事物**\n"
                "5. 譯文中若出現原文未提及的具體人名、團體名、作品名，視為幻覺，必須將 "
                "translation_status 設為 needs_review\n"
                "6. **ASR 諧音人名 / 殘留噪訊處理**：若原文出現疑似語音辨識誤判產生的"
                "「諧音自我介紹」、「聽眾雜訊」、「主播口頭禪被 ASR 當作人名」等片段（例："
                "「うるはるしあ自我紹介します」被 ASR 誤辨為「Vtuber一青空です」這類「原文"
                "有人名字串但語意與上下文不符」的情形），翻譯 zh_tw_text 時**不得將疑似 "
                "ASR 誤判的人名 / 團體名 / 作品名以任何形式（音譯、照翻、括號註記、注音）"
                "呈現在譯文中**，應直接以 generic 表述帶過（例如：「（自我介紹，語音辨識"
                "不確定）」），並將 translation_status 設為 needs_review"
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "required_schema": {
                        "zh_tw_text": "繁體中文翻譯",
                        "translation_status": "ok|needs_review",
                    },
                    "glossary_hits": glossary_hits,
                    "ja_text": segment.get("text", ""),
                    "ja_raw_text": segment.get("raw_text", ""),
                },
                ensure_ascii=False,
            ),
        },
    ]
    requested_max_tokens = max(512, len(segment.get("text", "")) * 3)
    try:
        result = client.chat_json(messages, max_tokens=min(client.max_tokens, requested_max_tokens))
    except TranslationMemoryResponseError as exc:
        enriched = exc.with_context(
            stage="translation",
            segment_index=segment.get("segment_index"),
            segment_uid=segment.get("segment_uid"),
            ja_text=segment.get("text", ""),
            ja_raw_text=segment.get("raw_text", ""),
            glossary_hits=deepcopy(glossary_hits),
        )
        client.record_response_failure(enriched.diagnostics)
        raise enriched from exc
    zh_tw_text = str(result.get("zh_tw_text") or "").strip()
    if not zh_tw_text:
        raise ValueError("Translation response missing zh_tw_text")
    status = str(result.get("translation_status") or "ok")
    if status not in {"ok", "needs_review"}:
        status = "needs_review"
    return {"zh_tw_text": zh_tw_text, "translation_status": status}


def _build_response_diagnostics(
    data: dict[str, Any],
    *,
    message: Any,
    finish_reason: Any,
    content: str,
    model: str,
    url: str,
    thinking: str,
    requested_max_tokens: int,
) -> dict[str, Any]:
    diagnostics = {
        "provider": "opencode",
        "model": model,
        "url": url,
        "thinking": thinking,
        "requested_max_tokens": requested_max_tokens,
        "response_id": data.get("id"),
        "finish_reason": finish_reason,
        "raw_message": deepcopy(message),
        "content_before_parse": content,
        "selected_text_source": "content",
    }
    if isinstance(message, dict):
        if "reasoning_content" in message:
            diagnostics["reasoning_content"] = message.get("reasoning_content")
        if "reasoning" in message:
            diagnostics["reasoning"] = deepcopy(message.get("reasoning"))
    return diagnostics


def _select_content_before_parse(diagnostics: dict[str, Any]) -> str:
    content = str(diagnostics.get("content_before_parse") or "")
    if content.strip():
        diagnostics["selected_text_source"] = "content"
        return content

    reasoning_content = diagnostics.get("reasoning_content")
    if isinstance(reasoning_content, str) and reasoning_content.strip():
        diagnostics["content_before_parse"] = reasoning_content
        diagnostics["selected_text_source"] = "reasoning_content"
        return reasoning_content

    reasoning = diagnostics.get("reasoning")
    if reasoning not in (None, ""):
        reasoning_text = _stringify_message_content(reasoning)
        if reasoning_text.strip():
            diagnostics["content_before_parse"] = reasoning_text
            diagnostics["selected_text_source"] = "reasoning"
            return reasoning_text

    diagnostics["selected_text_source"] = "content"
    return content


def _format_response_error(prefix: str, diagnostics: dict[str, Any]) -> str:
    finish_reason = diagnostics.get("finish_reason")
    content_len = len(str(diagnostics.get("content_before_parse") or ""))
    reasoning_len = _value_length(diagnostics.get("reasoning_content"))
    if reasoning_len == 0 and "reasoning" in diagnostics:
        reasoning_len = _value_length(diagnostics.get("reasoning"))
    parse_candidate_len = len(str(diagnostics.get("parse_candidate") or ""))
    parts = [prefix]
    if finish_reason is not None:
        parts.append(f"finish_reason={finish_reason}")
    parts.append(f"content_len={content_len}")
    if parse_candidate_len:
        parts.append(f"parse_candidate_len={parse_candidate_len}")
    if reasoning_len:
        parts.append(f"reasoning_len={reasoning_len}")
    if len(parts) == 1:
        return prefix
    return f"{prefix} ({', '.join(parts[1:])})"


def _stringify_message_content(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _value_length(value: Any) -> int:
    if value is None or value == "":
        return 0
    if isinstance(value, str):
        return len(value)
    return len(json.dumps(value, ensure_ascii=False))
