"""單一文字訊息、LINE Reply/Push HTTP adapter。"""

import base64
import hashlib
import hmac
from urllib.parse import urlencode

import httpx

from weather_bot.schemas import Reply

BASE = "https://api.line.me/v2/bot/message"


def verify_signature(body: bytes, signature: str, secret: str) -> bool:
    if not secret or not signature:
        return False
    expected = base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()
    return hmac.compare_digest(expected, signature)


def message_payload(reply: Reply, choice_set_id: str) -> dict:
    text = "\n".join(reply.sentences) + "\n\n" + "\n".join(f"{c.id}. {c.user_text}" for c in reply.choices)
    return {"messages": [{
        "type": "text", "text": text,
        "quickReply": {"items": [{"type": "action", "action": {
            "type": "postback", "label": c.label,
            "data": urlencode({"choice_set_id": choice_set_id, "choice_id": c.id}),
            "displayText": c.user_text,
        }} for c in reply.choices]},
    }]}


def fixed_payload(text: str) -> dict:
    return {"messages": [{"type": "text", "text": text}]}


class LineSender:
    def __init__(self, token: str, http: httpx.AsyncClient):
        self.http = http
        self.headers = {"Authorization": f"Bearer {token}"}

    async def reply(self, token: str, payload: dict):
        response = await self.http.post(f"{BASE}/reply", headers=self.headers,
                                        json={"replyToken": token, **payload}, timeout=8)
        response.raise_for_status()

    async def push(self, user_id: str, payload: dict, retry_key: str):
        response = await self.http.post(f"{BASE}/push",
            headers={**self.headers, "X-Line-Retry-Key": retry_key}, json={"to": user_id, **payload}, timeout=8)
        if response.status_code == 409 and response.headers.get("x-line-accepted-request-id"):
            return
        response.raise_for_status()

    async def quota(self):
        quota = await self.http.get(f"{BASE}/quota", headers=self.headers, timeout=8)
        quota.raise_for_status()
        usage = await self.http.get(f"{BASE}/quota/consumption", headers=self.headers, timeout=8)
        usage.raise_for_status()
        return quota.json().get("value"), usage.json()["totalUsage"]
