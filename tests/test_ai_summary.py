"""驗證 vLLM 請求契約與摘要流程；不呼叫外部服務。"""

import os
import unittest
from unittest.mock import patch

import requests

from weather_bot.ai_summary import (
    VLLMClient,
    WEATHER_SYSTEM_PROMPT,
    create_client_from_env,
    generate_weather_summary,
)


class VLLMClientTests(unittest.TestCase):
    def setUp(self):
        self.client = VLLMClient(
            api_key="test-key", url="https://example.test", model="BIRC-LLM"
        )
        self.post_patch = patch("weather_bot.ai_summary.requests.post")
        self.post = self.post_patch.start()
        self.addCleanup(self.post_patch.stop)
        self.post.return_value.json.return_value = {
            "choices": [{"message": {"content": "  天氣晴朗  "}}]
        }

    def test_url_normalization(self):
        for suffix in ("", "/", "/v1", "/v1/", "/v1/chat/completions/"):
            with self.subTest(suffix=suffix):
                client = VLLMClient("test-key", f"https://example.test{suffix}", "BIRC-LLM")
                self.assertEqual(client.url, "https://example.test/v1/chat/completions")

    def test_request_preserves_parameters_and_disables_thinking(self):
        messages = [{"role": "user", "content": "測試"}]
        self.assertEqual(self.client.chat(messages), "天氣晴朗")
        self.post.assert_called_once_with(
            "https://example.test/v1/chat/completions",
            headers={
                "Authorization": "Bearer test-key",
                "Content-Type": "application/json",
                "User-Agent": "weather-bot/1.0",
            },
            json={
                "model": "BIRC-LLM",
                "messages": messages,
                "temperature": 0.3,
                "max_tokens": 2048,
                "chat_template_kwargs": {"enable_thinking": False},
            },
            timeout=180.0,
        )

    def test_global_token_limit(self):
        self.client.max_tokens = 512
        self.client.chat([], max_tokens=1024)
        self.assertEqual(self.post.call_args.kwargs["json"]["max_tokens"], 512)

    def test_text_blocks(self):
        self.post.return_value.json.return_value = {
            "choices": [{"message": {"content": [
                {"type": "text", "text": "早安"}, "晴朗",
                {"type": "image_url", "image_url": "ignored"},
            ]}}]
        }
        self.assertEqual(self.client.chat([]), "早安\n晴朗")

    def test_empty_content_is_rejected_even_with_reasoning(self):
        for content in (None, "", "  ", []):
            with self.subTest(content=content):
                self.post.return_value.json.return_value = {
                    "choices": [{"message": {
                        "content": content, "reasoning_content": "不可推播的思考內容",
                    }}]
                }
                with self.assertRaisesRegex(ValueError, "空內容"):
                    self.client.chat([])

    def test_invalid_response_shape(self):
        for data in ({}, {"choices": []}, {"choices": [None]}, None):
            with self.subTest(data=data):
                self.post.return_value.json.return_value = data
                with self.assertRaisesRegex(ValueError, "格式異常"):
                    self.client.chat([])

    def test_non_text_content_is_rejected(self):
        self.post.return_value.json.return_value = {
            "choices": [{"message": {"content": {"unexpected": "data"}}}]
        }
        with self.assertRaisesRegex(ValueError, "不是文字"):
            self.client.chat([])

    def test_http_failure_is_propagated(self):
        self.post.return_value.raise_for_status.side_effect = requests.HTTPError("401")
        with self.assertRaises(requests.HTTPError):
            self.client.chat([])
        self.post.return_value.json.assert_not_called()


class EnvironmentTests(unittest.TestCase):
    @patch("weather_bot.ai_summary.load_dotenv")
    def test_new_settings_and_defaults(self, load_dotenv):
        with patch.dict(os.environ, {"AI_API_Keys": "test-key"}, clear=True):
            client = create_client_from_env()
        self.assertEqual(client.api_key, "test-key")
        self.assertEqual(client.url, "https://chatapi.ntubimdbirc.tw/v1/chat/completions")
        self.assertEqual(client.model, "BIRC-LLM")
        self.assertEqual(client.timeout, 180.0)
        self.assertEqual(client.max_tokens, 4096)

    @patch("weather_bot.ai_summary.load_dotenv")
    def test_custom_settings(self, load_dotenv):
        with patch.dict(os.environ, {
            "AI_API_Keys": "test-key", "AI_API_URL": "https://example.test/v1",
            "AI_MODEL": "custom", "AI_TIMEOUT": "30", "AI_MAX_TOKENS": "512",
        }, clear=True):
            client = create_client_from_env()
        self.assertEqual(client.url, "https://example.test/v1/chat/completions")
        self.assertEqual((client.model, client.timeout, client.max_tokens), ("custom", 30.0, 512))

    @patch("weather_bot.ai_summary.load_dotenv")
    def test_missing_key_does_not_fall_back_to_old_provider(self, load_dotenv):
        for key in ("", "  "):
            with self.subTest(key=key), patch.dict(os.environ, {
                "AI_API_Keys": key, "OPENCODE_GO_API_KEY": "old-test-key",
            }, clear=True), patch("weather_bot.ai_summary.requests.post") as post:
                with self.assertRaisesRegex(ValueError, "AI_API_Keys 未設定"):
                    create_client_from_env()
                post.assert_not_called()


class SummaryFlowTests(unittest.TestCase):
    @patch("weather_bot.ai_summary.create_client_from_env")
    def test_weather_summary_retains_prompt_data_and_token_limit(self, create_client):
        create_client.return_value.chat.return_value = "早安，天氣晴朗"
        weather = {"location": "臺北市", "forecasts": [{"weather": "晴朗"}]}
        self.assertEqual(generate_weather_summary(weather), "早安，天氣晴朗")
        args, kwargs = create_client.return_value.chat.call_args
        self.assertEqual(args[0][0], {"role": "system", "content": WEATHER_SYSTEM_PROMPT})
        self.assertIn('"weather": "晴朗"', args[0][1]["content"])
        self.assertEqual(kwargs, {"max_tokens": 1024})


if __name__ == "__main__":
    unittest.main()
