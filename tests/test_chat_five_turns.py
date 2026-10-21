from chat_scenario import ScenarioAI, run_five_turns
from conftest import Model, configured


class ContextualModel(Model):
    async def generate(self, system, data, *, memory=False):
        result = await super().generate(system, data, memory=memory)
        themes = ["今天最累的工作", "放下工作後的心情", "窗邊的熱可可", "窗外讓我放鬆的景色", "明天出門前的休息"]
        theme = themes[len(self.calls) - 1]
        result["choices"] = [
            {"id": "1", "label": "慢慢說給你聽", "user_text": f"我想聊聊{theme}，你陪我慢慢說完就好。"},
            {"id": "2", "label": "陪我放空一下", "user_text": f"想到{theme}，我想先放空一下，你陪在這裡就好。"},
            {"id": "3", "label": "換個角度聊聊", "user_text": f"關於{theme}，我想換個輕鬆的角度跟你聊聊。"},
        ]
        return result


async def test_T06_chat_five_turns_context_actions_and_reply_deadline(bot):
    await configured(bot)
    report = await run_five_turns(bot, ScenarioAI(ContextualModel()),
                                 output="logs/ci/chat_five_turns.json", model_name="deterministic-fake")
    assert report["status"] == "PASS", report
