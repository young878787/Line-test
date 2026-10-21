"""經過真實 n8n 引擎的整合測試；業務服務全部使用 HTTP mock。"""

import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import hmac
import json
from pathlib import Path
import sys
import time
import unittest

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))
from mcp_client import LocalN8n, LOGS, save
from build_workflows import unpack
from runtime_config import INTERNAL_HEADER, N8N_URL, TICK_PATH, internal_key
from weather_bot.regions import CITIES, DATASET

SECRET = b"n8n-mock-signature-secret"


class WorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mcp = LocalN8n()
        cls.manifest = json.loads((ROOT / "workflows/manifest.json").read_text(encoding="utf-8"))
        cls.http = httpx.Client(base_url="http://127.0.0.1:5682", headers={INTERNAL_HEADER: internal_key()}, timeout=30, trust_env=False)
        cls.pool = ThreadPoolExecutor(max_workers=2)

    def setUp(self):
        self.http.post("/mock/reset").raise_for_status()
        self.sequence = 0

    def state(self):
        return self.http.get("/mock/evidence").raise_for_status().json()

    def wait_for(self, predicate, timeout=8):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            state = self.state()
            if predicate(state):
                return state
            time.sleep(0.025)
        self.fail("等待資料狀態逾時")

    def event(self, text, uid="Utest", **fields):
        self.sequence += 1
        return {"type": "message", "webhookEventId": f"mock-{self.sequence}", "replyToken": f"mock-reply-{self.sequence}",
                "timestamp": 1792536600000, "source": {"type": "user", "userId": uid},
                "message": {"type": "text", "text": text}, **fields}

    def post(self, payload, *, signature=None, indent=None):
        body = json.dumps(payload, ensure_ascii=False, indent=indent).encode()
        signature = signature if signature is not None else base64.b64encode(hmac.new(SECRET, body, hashlib.sha256).digest()).decode()
        return httpx.post("http://127.0.0.1:5683/webhook", content=body,
                          headers={"content-type": "application/json", "x-line-signature": signature}, timeout=20, trust_env=False)

    def chat(self, text, uid="Utest", **fields):
        event = self.event(text, uid, **fields)
        r = self.post({"events": [event]})
        self.assertEqual(r.status_code, 200, r.text)
        key = "line:" + event["webhookEventId"]
        return self.wait_for(lambda s: any(j["business_key"] == key and j["status"] not in {"running", "pending"} for j in s["tables"]["jobs"]))

    def configured(self, uid="Utest", *, preference=False, city="新北市"):
        for text in (city, "07:00", "開啟晨報", "起床後再提醒"):
            state = self.chat(text, uid)
        self.assertEqual(next(u for u in state["tables"]["users"] if u["id"] == uid)["setup_step"], "preferences")
        if preference:
            self.chat("叫我海鹽布丁，口吻溫柔一點", uid)
        else:
            self.chat("先跳過", uid)
        return self.chat("確認開始", uid)

    def control(self, **values):
        self.http.post("/mock/control", json=values).raise_for_status()

    def start_background(self):
        return self.pool.submit(httpx.post, N8N_URL + TICK_PATH, headers={INTERNAL_HEADER: internal_key()}, timeout=20, trust_env=False)

    def finish_background(self, execution_id):
        response = execution_id.result(timeout=25)
        response.raise_for_status()
        self.assertEqual(response.json()["status"], "ok")
        return response.json()

    def background(self):
        self.finish_background(self.start_background())
        return self.state()

    def ai_calls(self, state):
        return sum(call["host"] == "ai.mock.invalid" for call in state["http_calls"])

    def patch_url(self, node, url):
        workflow_id = self.manifest["id"]
        self.mcp.call("update_workflow", {"workflowId": workflow_id,
                      "operations": [{"type": "setNodeParameter", "nodeName": node, "path": "/url", "value": url}],
                      "versionName": "Exercise local mock failure path"})
        detail = unpack(self.mcp.call("get_workflow_details", {"workflowId": workflow_id}))["workflow"]
        self.assertTrue(detail["connections"])
        self.mcp.call("publish_workflow", {"workflowId": workflow_id})

    def test_01_raw_body_utf8_and_whitespace(self):
        r = self.post({"events": [self.event("你好，世界！")]}, indent=3)
        self.assertEqual(r.status_code, 200)
        state = self.wait_for(lambda s: len(s["replies"]) == 1)
        self.assertIn("縣市", state["replies"][0]["payload"]["messages"][0]["text"])
        self.assertEqual(self.ai_calls(state), 0)

    def test_02_invalid_signature_rejected(self):
        r = self.post({"events": [self.event("你好")]}, signature="tampered")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.state()["tables"]["jobs"], [])

    def test_03_invalid_payload_rejected(self):
        for payload in ({}, {"events": {}}, {"events": ["invalid"]}):
            with self.subTest(payload=payload):
                self.assertEqual(self.post(payload).status_code, 400)
        self.assertEqual(self.state()["tables"]["users"], [])

    def test_04_body_size_limit(self):
        self.assertEqual(self.post({"events": [], "padding": "x" * (1024 * 1024)}).status_code, 413)

    def test_05_concurrent_redelivery_is_idempotent(self):
        payload = {"events": [self.event("你好")]}
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.post(payload).status_code, range(4)))
        self.assertEqual(results, [200] * 4)
        state = self.wait_for(lambda s: len(s["replies"]) == 1)
        self.assertEqual(len(state["tables"]["jobs"]), 1)
        self.assertEqual(len(state["tables"]["outbox"]), 1)

    def test_06_onboarding_requires_confirmation_and_saves_sources(self):
        state = self.configured(preference=True)
        user = state["tables"]["users"][0]
        self.assertEqual((user["setup_step"], user["city"], user["wake_time"]), ("done", "新北市", "07:00"))
        self.assertIsNone(user["district"])
        self.assertEqual((user["dataset"], user["location_code"]), (DATASET, CITIES["新北市"]))
        self.assertEqual({m["key"] for m in state["tables"]["memories"]}, {"persona.address_user_as", "persona.style"})
        self.assertTrue(all(json.loads(m["source_message_ids"]) for m in state["tables"]["memories"]))
        self.assertEqual(state["pushes"], [])

    def test_07_chat_one_message_three_actions(self):
        self.configured(preference=True)
        state = self.chat("今天有點累")
        messages = state["replies"][-1]["payload"]["messages"]
        self.assertEqual(len(messages), 1)
        self.assertEqual(len(messages[0]["quickReply"]["items"]), 3)
        self.assertIn("海鹽布丁", messages[0]["text"])

    def test_08_weather_uses_mock_facts(self):
        self.configured()
        state = self.chat("今天下午天氣如何")
        text = state["replies"][-1]["payload"]["messages"][0]["text"]
        self.assertIn("新北市", text)
        self.assertNotIn("None", text)
        self.assertIn("降雨機率", text)
        self.assertTrue(state["tables"]["weather_snapshots"])

    def test_09_numeric_choice_uses_full_text(self):
        self.configured()
        self.chat("今天辛苦了")
        state = self.chat("1")
        users = [m for m in state["tables"]["messages"] if m["role"] == "user" and m["kind"] == "chat"]
        self.assertEqual(users[-1]["content"], "陪我聊聊今天的安排")

    def test_10_expired_choice_does_not_call_ai(self):
        self.configured()
        before = self.chat("今天辛苦了")
        state = self.chat("", type="postback", postback={"data": "choice_set_id=expired&choice_id=1"})
        self.assertIn("過期", state["replies"][-1]["payload"]["messages"][0]["text"])
        self.assertEqual(self.ai_calls(before), self.ai_calls(state))

    def test_11_outside_scope_does_not_call_ai(self):
        self.configured()
        before = self.ai_calls(self.state())
        state = self.chat("幫我寫一個Python程式")
        self.assertIn("代寫", state["replies"][-1]["payload"]["messages"][0]["text"])
        self.assertEqual(self.ai_calls(state), before)

    def test_12_ai_failure_uses_short_fallback(self):
        self.configured()
        self.control(ai_failure=True)
        state = self.chat("今天辛苦了")
        self.assertIn("迷糊", state["replies"][-1]["payload"]["messages"][0]["text"])
        self.assertEqual(state["tables"]["jobs"][-1]["error"], "HTTPStatusError")

    def test_13_uncertain_reply_is_not_retried_or_pushed(self):
        self.configured()
        self.control(reply_failure=True)
        event = self.event("今天辛苦了")
        self.assertEqual(self.post({"events": [event]}).status_code, 200)
        state = self.wait_for(lambda s: s["tables"]["jobs"][-1]["status"] == "uncertain")
        calls = len([c for c in state["http_calls"] if c["path"].endswith("/reply")])
        self.post({"events": [event]})
        self.background()
        state = self.state()
        self.assertEqual(len([c for c in state["http_calls"] if c["path"].endswith("/reply")]), calls)
        self.assertEqual(state["pushes"], [])

    def test_14_cwa_failure_keeps_valid_county_setting(self):
        self.control(cwa_failure=True)
        state = self.chat("臺中市")
        self.assertEqual(state["tables"]["users"][0]["setup_step"], "wake")
        self.assertIn("臺中市", state["replies"][-1]["payload"]["messages"][0]["text"])
        self.assertFalse(any(c["host"] == "opendata.cwa.gov.tw" for c in state["http_calls"]))

    def test_15_background_memory_preserves_sources(self):
        self.configured()
        self.chat("記住叫我海鹽布丁，口吻溫柔一點")
        state = self.background()
        self.assertEqual({m["key"] for m in state["tables"]["memories"]}, {"persona.address_user_as", "persona.style"})
        self.assertTrue(all(j["status"] == "done" for j in state["tables"]["jobs"] if j["kind"] == "memory"))

    def test_16_morning_six_sentences_and_no_duplicate_push(self):
        self.configured()
        state = self.background()
        self.assertEqual(state["pushes"], [])
        self.control(advance_seconds=600)
        state = self.background()
        self.assertEqual(len(state["pushes"]), 1)
        text = state["pushes"][0]["payload"]["messages"][0]["text"]
        self.assertEqual(len(text.split("\n\n")[0].splitlines()), 6)
        self.assertEqual(len(self.background()["pushes"]), 1)

    def test_17_unfollow_stops_processing(self):
        self.configured()
        before = len(self.state()["replies"])
        self.post({"events": [self.event("", type="unfollow")]})
        self.chat("今天辛苦了")
        state = self.state()
        self.assertEqual(state["tables"]["users"][0]["active"], 0)
        self.assertEqual(len(state["replies"]), before)

    def test_18_user_memory_isolation(self):
        self.configured("Ufirst", preference=True)
        self.configured("Usecond")
        state = self.chat("今天辛苦了", "Usecond")
        self.assertNotIn("海鹽布丁", state["replies"][-1]["payload"]["messages"][0]["text"])
        self.assertTrue(all(m["user_id"] == "Ufirst" for m in state["tables"]["memories"]))

    def test_19_slow_memory_does_not_block_chat_reply(self):
        self.configured()
        self.chat("記住叫我海鹽布丁")
        self.control(memory_delay=2)
        execution = self.start_background()
        self.wait_for(lambda s: any(j["kind"] == "memory" and j["status"] == "running" for j in s["tables"]["jobs"]))
        state = self.chat("先陪我聊一下")
        self.assertTrue(any(j["kind"] == "memory" and j["status"] == "running" for j in state["tables"]["jobs"]))
        self.assertLess(state["replies"][-1]["elapsed_seconds"], 2)
        self.finish_background(execution)

    def test_20_compaction_preserves_recent_turns(self):
        self.configured()
        for i in range(20):
            self.chat(f"陪我聊聊今天的安排第{i}輪")
        state = self.background()
        conversation = state["tables"]["conversation_state"][0]
        self.assertGreater(conversation["summary_through"], 0)
        self.assertEqual(len([m for m in state["tables"]["messages"] if m["kind"] == "chat" and m["id"] > conversation["summary_through"]]), 10)

    def test_21_adapter_transport_failure_returns_503(self):
        try:
            self.patch_url("Verify and Persist", "http://127.0.0.1:1/ingress")
            response = self.post({"events": []})
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json(), {"error": "adapter_unavailable"})
        finally:
            self.patch_url("Verify and Persist", "http://127.0.0.1:5682/ingress")

    def test_22_workflow_error_is_captured(self):
        try:
            self.patch_url("Process Chat Jobs", "http://127.0.0.1:1/jobs/chats")
            self.assertEqual(self.post({"events": []}).status_code, 200)
            state = self.wait_for(lambda s: bool(s["errors"]))
            self.assertEqual(state["errors"][-1]["node"], "Process Chat Jobs")
            self.assertEqual(set(state["errors"][-1]), {"workflow_id", "execution_id", "node"})
        finally:
            self.patch_url("Process Chat Jobs", "http://127.0.0.1:5682/jobs/chats")

    def test_23_mcp_pinned_mock_executes_response_expression(self):
        result = self.mcp.call("test_workflow", {"workflowId": self.manifest["id"], "triggerNodeName": "LINE Raw Webhook",
                      "pinData": {"LINE Raw Webhook": [{"json": {"headers": {}, "body": {"events": []}}}],
                                  "Verify and Persist": [{"json": {"statusCode": 400, "body": {"detail": "Invalid signature"}}}],
                                  "Process Chat Jobs": [{"json": {"status": "ok"}}]}, "timeout": 20})
        value = unpack(result)
        save(LOGS / "pinned_test.json", value)
        self.assertEqual(value.get("status"), "success", str(value))
        self.assertEqual(self.state()["http_calls"], [])

    def test_24_rain_change_alert_is_sent_once(self):
        self.configured()
        self.chat("今天天氣如何？")
        self.control(weather_rain=True, advance_seconds=3000)
        state = self.background()
        alerts = [r for r in state["tables"]["outbox"] if r["kind"] == "weather_alert"]
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["status"], "sent")
        self.assertIn("預報時段變更", state["pushes"][0]["payload"]["messages"][0]["text"])
        self.assertEqual(len(self.background()["pushes"]), 1)

    def test_25_push_retry_keeps_the_same_idempotency_key(self):
        self.configured()
        self.background()
        self.control(advance_seconds=600, push_failures=1)
        state = self.background()
        row = next(r for r in state["tables"]["outbox"] if r["method"] == "push")
        self.assertEqual((row["status"], row["attempts"]), ("pending", 1))
        retry_key = row["retry_key"]
        self.control(advance_seconds=30)
        state = self.background()
        self.assertEqual(len(state["pushes"]), 1)
        self.assertEqual(state["pushes"][0]["retry_key"], retry_key)
        row = next(r for r in state["tables"]["outbox"] if r["method"] == "push")
        self.assertEqual((row["status"], row["attempts"]), ("sent", 2))

    def test_26_disable_morning_cancels_pending_push(self):
        self.configured()
        self.background()
        self.chat("關閉晨報")
        self.control(advance_seconds=600)
        state = self.background()
        self.assertEqual(state["pushes"], [])
        self.assertEqual(next(r for r in state["tables"]["outbox"] if r["method"] == "push")["status"], "cancelled")

    def persist_without_processing(self):
        body = json.dumps({"events": [self.event("你好")]}, ensure_ascii=False).encode()
        signature = base64.b64encode(hmac.new(SECRET, body, hashlib.sha256).digest()).decode()
        self.http.post("/ingress", content=body, headers={"x-line-signature": signature}).raise_for_status()

    def test_27_schedule_recovers_persisted_pending_chat(self):
        # 模擬已 ACK 入庫，原 n8n 執行卻未繼續處理；只能由每秒補跑領取。
        self.persist_without_processing()
        state = self.wait_for(lambda s: bool(s["replies"]) and s["tables"]["jobs"][0]["status"] == "done", timeout=5)
        self.assertEqual(len(state["replies"]), 1)
        self.assertEqual(state["tables"]["jobs"][0]["status"], "done")

    def test_28_expired_persisted_job_does_not_use_reply_token(self):
        workflow_id = self.manifest["id"]
        self.mcp.call("unpublish_workflow", {"workflowId": workflow_id})
        try:
            self.persist_without_processing()
            self.control(advance_seconds=56)
        finally:
            self.mcp.call("publish_workflow", {"workflowId": workflow_id})
        state = self.wait_for(lambda s: s["tables"]["jobs"][0]["status"] == "expired", timeout=5)
        self.assertEqual(state["replies"], [])
        self.assertEqual(state["tables"]["outbox"], [])

    def test_29_all_counties_setup_and_forecast(self):
        for city, code in CITIES.items():
            with self.subTest(city=city):
                uid = "Ucounty" + code
                state = self.configured(uid, city=city)
                user = next(u for u in state["tables"]["users"] if u["id"] == uid)
                self.assertEqual((user["city"], user["dataset"], user["location_code"]), (city, DATASET, code))
                self.assertIsNone(user["district"])
                state = self.chat("今天天氣如何？", uid)
                text = state["replies"][-1]["payload"]["messages"][0]["text"]
                self.assertIn(city, text)
                self.assertNotIn("None", text)
                self.assertEqual(state["external_network_calls"], 0)

    def test_30_query_county_preserves_home_and_ambiguous_city_is_required(self):
        self.configured(city="臺北市")
        state = self.chat("高雄市今天天氣如何？")
        text = state["replies"][-1]["payload"]["messages"][0]["text"]
        self.assertIn("高雄市", text)
        self.assertNotIn("臺北市", text)
        self.assertEqual(state["tables"]["users"][0]["city"], "臺北市")
        state = self.chat("嘉義明天天氣如何？")
        text = state["replies"][-1]["payload"]["messages"][0]["text"]
        self.assertIn("嘉義縣", text)
        self.assertIn("嘉義市", text)

    def test_31_short_answers_and_completed_corrections(self):
        for text in ("澎湖縣", "早上七點半", "要", "不要", "先跳過", "確認開始"):
            state = self.chat(text)
        user = state["tables"]["users"][0]
        self.assertEqual((user["city"], user["wake_time"], user["morning_enabled"], user["alerts_enabled"], user["night_forecast_enabled"]),
                         ("澎湖縣", "07:30", 1, 1, 0))
        for text in ("晨報時間改成早上八點半", "改住臺中市", "叫我阿洋"):
            state = self.chat(text)
        user = state["tables"]["users"][0]
        self.assertEqual((user["city"], user["wake_time"], user["setup_step"]), ("臺中市", "08:30", "done"))
        self.assertEqual(state["tables"]["memories"][0]["value"], "阿洋")
        for text, enabled in (("夜間還是可以提醒我", 1), ("夜間不可以提醒我", 0)):
            state = self.chat(text)
            self.assertEqual(state["tables"]["users"][0]["night_forecast_enabled"], enabled)
        state = self.chat("改叫我小洋")
        self.assertEqual(state["tables"]["memories"][0]["value"], "小洋")
        state = self.chat("今天好累")
        self.assertEqual(state["tables"]["outbox"][-1]["kind"], "chat")
        self.assertEqual(state["external_network_calls"], 0)

    def test_32_same_day_forecast_changes_are_not_limited_to_one(self):
        self.configured()
        self.chat("關閉晨報")
        self.chat("今天天氣如何？")
        self.control(weather_rain=True, advance_seconds=3000)
        self.background()
        self.control(weather_rain=False, advance_seconds=3600)
        state = self.background()
        self.assertEqual(len(state["pushes"]), 2)
        self.assertEqual(len({r["retry_key"] for r in state["pushes"]}), 2)
        self.assertTrue(all(r["status"] == "done" for r in state["tables"]["jobs"] if r["kind"] == "weather_change"))

    def test_33_night_deferral_survives_until_wake(self):
        self.configured()
        self.chat("關閉晨報")
        self.chat("今天天氣如何？")
        self.control(weather_rain=True, advance_seconds=16 * 3600 + 50 * 60)
        state = self.background()
        self.assertEqual(state["pushes"], [])
        deferred = [r for r in state["tables"]["jobs"] if r["kind"] == "weather_change"]
        self.assertEqual(len(deferred), 1)
        self.assertEqual(deferred[0]["status"], "pending")
        self.control(advance_seconds=7 * 3600 + 20 * 60)
        state = self.background()
        self.assertEqual(len(state["pushes"]), 1)
        response = self.finish_background(self.start_background())
        self.assertIn("jobs", response)
        self.assertIn("notices", response)

    @classmethod
    def tearDownClass(cls):
        cls.pool.shutdown()
        cls.http.close()
        cls.mcp.http.close()


class JsonResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cases = []
        self.subtest_errors = {}

    def addSubTest(self, test, subtest, err):
        if err is not None:
            self.subtest_errors[test] = self._exc_info_to_string(err, test)
        super().addSubTest(test, subtest, err)

    def startTest(self, test):
        self.started = time.monotonic()
        super().startTest(test)

    def stopTest(self, test):
        failures = self.failures + self.errors
        error = next((text for case, text in failures if case == test), self.subtest_errors.get(test))
        state = test.state()
        evidence_path = LOGS / "cases" / f"{test._testMethodName}.json"
        save(evidence_path, state)
        self.cases.append({"id": test._testMethodName, "status": "failed" if error else "passed",
                           "seconds": round(time.monotonic() - self.started, 3), "error": error,
                           "reply_count": len(state["replies"]), "push_count": len(state["pushes"]),
                           "external_network_calls": state["external_network_calls"],
                           "evidence": evidence_path.relative_to(ROOT).as_posix()})
        super().stopTest(test)


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(WorkflowTests)
    result = unittest.TextTestRunner(stream=sys.stdout, verbosity=2, resultclass=JsonResult).run(suite)
    save(LOGS / "latest.json", {"status": "passed" if result.wasSuccessful() else "failed", "total": result.testsRun,
                                 "passed": sum(c["status"] == "passed" for c in result.cases),
                                 "failed": sum(c["status"] == "failed" for c in result.cases), "mode": "mock", "cases": result.cases})
    sys.exit(0 if result.wasSuccessful() else 1)
