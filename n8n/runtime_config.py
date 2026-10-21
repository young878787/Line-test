"""獨立實例的固定位置、內部憑證與啟動設定。"""

import json
import os
from pathlib import Path
import secrets

from dotenv import dotenv_values
from weather_bot.config import Settings

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / ".runtime"
N8N_URL = "http://127.0.0.1:5680"
ADAPTER_URL = "http://127.0.0.1:5682"
GATEWAY_URL = "http://127.0.0.1:5683"
WEBHOOK_PATH = "/webhook/line-weather"
TICK_PATH = "/webhook/line-weather-tick"
INTERNAL_HEADER = "X-Line-N8n-Key"
MOCK_SECRET = "n8n-mock-signature-secret"


def internal_key():
    RUNTIME.mkdir(parents=True, exist_ok=True)
    path = RUNTIME / "internal-key.json"
    if not path.exists():
        try:
            with path.open("x", encoding="utf-8") as output:
                json.dump({"key": secrets.token_urlsafe(48)}, output)
        except FileExistsError:
            pass
    return json.loads(path.read_text(encoding="utf-8"))["key"]


def load_settings(mock=False):
    if mock:
        return Settings(database=str(RUNTIME / "mock_bot.sqlite3"), line_secret=MOCK_SECRET,
                        line_token="mock-line", cwa_key="mock-cwa", ai_key="mock-ai",
                        ai_url="https://ai.mock.invalid", ai_model="mock-model", local_tunnel_enabled=False)
    values = {**dotenv_values(ROOT.parent / ".env"), **os.environ}
    required = ("LINE_CHANNEL_ACCESS_TOKEN", "LINE_CHANNEL_SECRET", "CWA_API_KEY", "AI_API_Keys")
    missing = [key for key in required if not values.get(key)]
    if missing:
        raise ValueError("缺少設定：" + ", ".join(missing))
    return Settings(database=values.get("DATABASE_PATH") or "bot.sqlite3",
                    line_secret=values["LINE_CHANNEL_SECRET"], line_token=values["LINE_CHANNEL_ACCESS_TOKEN"],
                    cwa_key=values["CWA_API_KEY"], ai_key=values["AI_API_Keys"],
                    ai_url=values.get("AI_API_URL") or "https://chatapi.ntubimdbirc.tw",
                    ai_model=values.get("AI_MODEL") or "BIRC-LLM",
                    monthly_limit=int(values.get("LINE_MONTHLY_LIMIT") or 200), local_tunnel_enabled=False)
