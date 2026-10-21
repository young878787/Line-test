"""服務設定；金鑰僅在程序啟動時載入。"""

from dataclasses import dataclass, field
from datetime import datetime
import os
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

TZ = ZoneInfo("Asia/Taipei")
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Clock:
    def now(self) -> datetime:
        return datetime.now(TZ)

    def monotonic(self) -> float:
        return time.monotonic()


@dataclass
class Settings:
    database: str = "bot.sqlite3"
    line_secret: str = field(default="", repr=False)
    line_token: str = field(default="", repr=False)
    cwa_key: str = field(default="", repr=False)
    ai_key: str = field(default="", repr=False)
    ai_url: str = "https://chatapi.ntubimdbirc.tw"
    ai_model: str = "BIRC-LLM"
    chat_timeout: float = 25.0
    memory_timeout: float = 90.0
    reply_timeout: float = 55.0
    monthly_limit: int = 200
    input_budget: int = 6000
    local_tunnel_enabled: bool = True

    def __post_init__(self):
        if self.database != ":memory:":
            self.database = str((PROJECT_ROOT / self.database).resolve())
        if not 0 < self.chat_timeout < self.reply_timeout <= 55:
            raise ValueError("聊天期限必須小於回覆期限，回覆期限最多 55 秒")

    @classmethod
    def from_env(cls):
        load_dotenv()
        return cls(
            database=os.getenv("DATABASE_PATH") or "bot.sqlite3",
            line_secret=os.getenv("LINE_CHANNEL_SECRET", ""),
            line_token=os.getenv("LINE_CHANNEL_ACCESS_TOKEN", ""),
            cwa_key=os.getenv("CWA_API_KEY", ""),
            ai_key=os.getenv("AI_API_Keys", ""),
            ai_url=os.getenv("AI_API_URL") or "https://chatapi.ntubimdbirc.tw",
            ai_model=os.getenv("AI_MODEL") or "BIRC-LLM",
            monthly_limit=int(os.getenv("LINE_MONTHLY_LIMIT", "200")),
            local_tunnel_enabled=os.getenv("LOCAL_TUNNEL_ENABLED", "true").lower() in ("1", "true", "yes"),
        )
