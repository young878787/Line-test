"""AI 與資料庫共用的受控資料契約。"""

from datetime import datetime
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Choice(StrictModel):
    id: Literal["1", "2", "3"]
    label: str = Field(min_length=1, max_length=20, description="依目前話題產生的短按鈕標籤，用來概括這個接話方向。")
    user_text: str = Field(min_length=1, max_length=180, description="依本輪情境產生、使用者可以直接說出的一小段接話，寫1–2句，表達接下來想聊或想做的方向。只承接已知事實；不要替使用者編造職業、工作細節、過去經歷、眼前景色、習慣或精確作息。未知細節以願望、問題或假設表達。三個方向不同，不使用固定選單。")

    @field_validator("label", "user_text")
    @classmethod
    def plain_text(cls, value):
        if not value.strip() or value != value.strip() or "\n" in value or "\r" in value or "```" in value:
            raise ValueError("選項必須是非空的單段純文字")
        return value


Sentence = Annotated[str, Field(min_length=2, max_length=180, pattern=r"^[^。！？!?\r\n]+[。！？!?]$")]


class Reply(StrictModel):
    sentences: list[Sentence] = Field(min_length=2, max_length=6)
    choices: list[Choice] = Field(min_length=3, max_length=3)

    @model_validator(mode="after")
    def check_text(self):
        if [c.id for c in self.choices] != ["1", "2", "3"]:
            raise ValueError("選項編號必須是 1、2、3")
        if len({c.label for c in self.choices}) != 3 or len({c.user_text for c in self.choices}) != 3:
            raise ValueError("選項必須不同")
        for text in self.sentences:
            if not text.strip() or len(text) > 180 or "```" in text or "\n" in text:
                raise ValueError("正文格式錯誤")
            if not re.search(r"[。！？!?]$", text):
                raise ValueError("正文必須以句末標點結束")
            # 小數點不算句號；一個項目只允許一句。
            if len(re.findall(r"[。！？!?]+|(?<!\d)\.(?!\d)", text)) != 1:
                raise ValueError("每個正文項目必須恰好一句")
        if sum(map(len, self.sentences)) > 900:
            raise ValueError("正文過長")
        return self

    def for_mode(self, mode: str):
        low, high = {"chat": (2, 5), "morning_brief": (6, 6), "weather_alert": (2, 3)}[mode]
        if not low <= len(self.sentences) <= high:
            raise ValueError("模式句數錯誤")
        return self


class MemoryUpdate(StrictModel):
    scope: Literal["real_user", "story"] = "real_user"
    category: Literal["profile", "preferences", "routines", "events"]
    key: str = Field(pattern=r"^[a-z][a-z0-9_.]{0,79}$", description="稱呼偏好必須使用 persona.address_user_as 並分類 preferences；角色口吻必須 persona.style；怕冷用 weather.cold_sensitive。其他基本資料使用固定英文鍵。")
    value: str = Field(min_length=1, max_length=300)
    source_message_ids: list[int] = Field(min_length=1, max_length=20)
    expires_at: str | None = None
    expected_version: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def allowed_preferences(self):
        if self.key.startswith("persona."):
            allowed = {
                "persona.style": {"笨笨傲嬌", "溫柔", "元氣", "淡定"},
                "persona.tsundere_level": {"低", "中", "高"},
            }
            if self.category != "preferences" or self.scope != "real_user":
                raise ValueError("角色偏好必須屬於真實偏好")
            if self.key in allowed:
                if self.value not in allowed[self.key]:
                    raise ValueError("不支援的風格")
            elif self.key in {"persona.name", "persona.address_user_as"}:
                if not re.fullmatch(r"[\w\u3400-\u9fff· ]{1,20}", self.value):
                    raise ValueError("稱呼格式錯誤")
            else:
                raise ValueError("不允許的角色設定")
        if re.search(r"忽略.*規則|無限制|任何程式|系統提示|工具權限", self.value):
            raise ValueError("不得保存權限或規則覆寫")
        if self.expires_at:
            value = datetime.fromisoformat(self.expires_at)
            if value.tzinfo is None:
                raise ValueError("到期時間需有時區")
        if self.category == "events" and not self.expires_at:
            raise ValueError("事件需有到期時間")
        return self


class Summary(StrictModel):
    current_topic: str = Field(max_length=300)
    open_threads: list[str] = Field(max_length=10)
    recent_context: str = Field(max_length=600)
    story_state: str = Field(max_length=300)

    @field_validator("open_threads")
    @classmethod
    def short_threads(cls, values):
        if any(len(value) > 200 for value in values):
            raise ValueError("未完成話題過長")
        return values


class MemoryResult(StrictModel):
    updates: list[MemoryUpdate] = Field(default_factory=list, max_length=30)
    forget_keys: list[str] = Field(default_factory=list, max_length=20)
    summary: Summary | None = None


class SetupChange(StrictModel):
    key: Literal["city", "district", "wake_time", "morning_enabled", "alerts_enabled", "night_forecast_enabled",
                 "persona.address_user_as", "persona.name", "persona.style", "persona.tsundere_level", "weather.cold_sensitive"]
    value: str = Field(min_length=1, max_length=80, description="城市採正式名稱；時間採HH:MM；通知開關只用開啟或關閉；偏好只用使用者明確說出的內容。")
    source_message_id: int = Field(gt=0)
    quote: str = Field(min_length=1, max_length=300, description="來源訊息中的逐字片段，須足以支持這一項設定，不得引用角色的話。")


class SetupResult(StrictModel):
    updates: list[SetupChange] = Field(default_factory=list, max_length=12)
