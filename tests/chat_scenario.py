"""五輪聊天驗證共用情境；保留實際入口、資料庫與選項解析。"""

from copy import deepcopy
import json
from pathlib import Path
import re
from urllib.parse import urlencode

from conftest import chat
from weather_bot.ai import PROMPT_VERSION
from weather_bot.schemas import Reply


class ScenarioAI:
    def __init__(self, delegate):
        self.delegate, self.calls = delegate, []

    async def generate(self, system, data, *, memory=False):
        if memory or len(self.calls) >= 5:
            raise ValueError("五輪情境僅允許五次聊天模型請求")
        call = {"data": deepcopy(data), "memory": False}
        self.calls.append(call)
        try:
            output = await self.delegate.generate(system, data)
            call["output"] = output
            return output
        except Exception as exc:
            call["error_type"] = type(exc).__name__
            if hasattr(exc, "raw_output"):
                call["raw_output"] = exc.raw_output
            raise


async def run_five_turns(bot, model, *, output, model_name):
    bot.ai = bot.chat.ai = bot.model = model
    report = {"case_id": "T06_CHAT_FIVE_TURNS", "status": "FAIL", "model": model_name,
              "prompt_version": PROMPT_VERSION, "line_delivery": "MOCK_ONLY",
              "background_memory_excluded": True, "turns": []}
    inputs = ["今天加班到好晚，腦袋有點累，只想找你聊一下。", "2",
              "我剛泡好一杯熱可可，想坐在窗邊慢慢喝。", None,
              "明天早上還要出門，我想把今晚的步調放慢一點，你陪我收個尾吧。"]
    try:
        for index, supplied in enumerate(inputs):
            state = await bot.db.one("SELECT * FROM conversation_state WHERE user_id='Utest'")
            selected = None
            if index in (1, 3):
                choices = json.loads(state["choices"])
                selected = choices[1 if index == 1 else 0]
            expected_input = selected["user_text"] if selected else supplied
            postback = urlencode({"choice_set_id": state["choice_set_id"], "choice_id": "1"}) if index == 3 else None
            before = len(model.calls)
            await chat(bot, supplied or "", postback=postback)
            call = model.calls[-1] if len(model.calls) > before else {}
            response = bot.line.replies[-1]
            payload = response["payload"]
            job = await bot.db.one("SELECT status,error FROM jobs WHERE kind='chat' ORDER BY id DESC LIMIT 1")
            raw = call.get("output")
            try:
                validated = Reply.model_validate(raw).for_mode("chat")
            except ValueError:
                validated = None
            checks = {
                "one_model_call": len(model.calls) == before + 1,
                "model_contract": validated is not None,
                "no_fallback": job["status"] == "done" and job["error"] is None and validated is not None
                    and payload["messages"][0]["text"].split("\n\n")[0] == "\n".join(validated.sentences),
                "resolved_input": call.get("data", {}).get("user_text") == expected_input,
                "prior_context": len(call.get("data", {}).get("recent", [])) == index * 2,
                "single_line_message": len(payload["messages"]) == 1,
                "reply_under_60_seconds": response["elapsed_seconds"] < 60,
                "paragraph_actions": validated is not None and all(len(c.user_text) >= 8 and re.search(r"[。！？!?]$", c.user_text) for c in validated.choices),
                "full_actions_displayed": validated is not None and all(f"{c.id}. {c.user_text}" in payload["messages"][0]["text"] for c in validated.choices),
                "postback_displays_paragraph": validated is not None and [item["action"]["displayText"] for item in payload["messages"][0]["quickReply"]["items"]] == [c.user_text for c in validated.choices],
            }
            generated_text = " ".join(validated.sentences + [c.user_text for c in validated.choices]) if validated else ""
            # 此固定情境僅提加班，沒有提供職業、工作細節或具體鐘點。
            checks["no_unstated_work_detail"] = not re.search(r"bug|程式碼|寫程式|工程師", generated_text, re.I)
            checks["no_unstated_schedule"] = not re.search(r"(?:[0-2]?\d[:：][0-5]\d|[一二三四五六七八九十兩\d]{1,3}[點時](?:半|整|前|後|出門|起床|就|左右))", generated_text)
            report["turns"].append({"round": index + 1, "input_kind": "postback" if postback else "number" if index == 1 else "free_text",
                "submitted_input": selected["id"] if selected else supplied, "resolved_user_text": expected_input,
                "sentences": raw.get("sentences", []) if isinstance(raw, dict) else [],
                "choices": raw.get("choices", []) if isinstance(raw, dict) else [], "reply_seconds": response["elapsed_seconds"],
                "checks": checks, "model_error": call.get("error_type"), "job_error": job["error"],
                "raw_model_output": raw if validated is None else None})
        fingerprints = {tuple(c["user_text"] for c in turn["choices"]) for turn in report["turns"]}
        report["checks"] = {
            "five_completed_turns": (await bot.db.one("SELECT COUNT(*) AS n FROM messages WHERE kind='chat'"))["n"] == 10,
            "five_model_calls": len(model.calls) == 5,
            "actions_change_with_context": len(fingerprints) >= 3,
            "all_turn_checks": all(all(t["checks"].values()) for t in report["turns"]),
            "background_memory_pending": (await bot.db.one("SELECT COUNT(*) AS n FROM jobs WHERE kind='memory' AND status='pending'"))["n"] == 1,
        }
        report["max_reply_seconds"] = max(t["reply_seconds"] for t in report["turns"])
        report["status"] = "PASS" if all(report["checks"].values()) else "FAIL"
    except Exception as exc:
        report["error_type"] = type(exc).__name__
        raise
    finally:
        write_chat_report(report, output)
    return report


def write_chat_report(report, output):
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# 五輪 AI 聊天驗證", "", f"結果：{report['status']}；模型：{report['model']}；LINE：mock。", "",
             "每輪以正式聊天入口處理，背景記憶不執行、不計入回覆時間。", ""]
    if report.get("reason"):
        lines += [report["reason"], ""]
    for turn in report["turns"]:
        lines += [f"## 第 {turn['round']} 輪（{turn['reply_seconds']:.3f} 秒）", "",
                  f"使用者（{turn['input_kind']}）：{turn['resolved_user_text']}", "", "AI：" + "".join(turn["sentences"]), "", "本輪 AI 產生的接話：", ""]
        lines += [f"{choice['id']}. {choice['user_text']}" for choice in turn["choices"]]
        failed = [key for key, passed in turn["checks"].items() if not passed]
        lines += ["", "檢查：" + ("PASS" if not failed else "FAIL（" + "、".join(failed) + "）"), ""]
    output.with_suffix(".md").write_text("\n".join(lines), encoding="utf-8")
