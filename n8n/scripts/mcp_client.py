"""僅連線至此專案的本機 n8n；敏感資料只存於忽略版控的 .runtime。"""

import json
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:5680"
RUNTIME = ROOT / ".runtime"
LOGS = ROOT / "logs"


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class LocalN8n:
    def __init__(self):
        self.http = httpx.Client(base_url=BASE, timeout=120, trust_env=False, limits=httpx.Limits(keepalive_expiry=2))
        account_path = RUNTIME / "account.json"
        if not account_path.exists():
            import secrets
            save(account_path, {"email": "line-test@example.test", "password": "Aa1!" + secrets.token_urlsafe(32),
                                "firstName": "LINE", "lastName": "Mock"})
        self.account = json.loads(account_path.read_text(encoding="utf-8"))
        status = self.http.get("/rest/settings").raise_for_status().json()["data"]
        if status["userManagement"]["showSetupOnFirstLoad"]:
            self.http.post("/rest/owner/setup", json=self.account).raise_for_status()
        else:
            self.http.post("/rest/login", json={"emailOrLdapLoginId": self.account["email"],
                                               "password": self.account["password"]}).raise_for_status()
        self.http.patch("/rest/mcp/settings", json={"mcpAccessEnabled": True}).raise_for_status()
        key_path = RUNTIME / "mcp-key.json"
        key = json.loads(key_path.read_text(encoding="utf-8")) if key_path.exists() else {}
        if not key.get("apiKey") or "*" in key["apiKey"]:
            key = self.http.get("/rest/mcp/api-key").raise_for_status().json()["data"]
            if "*" in key["apiKey"]:
                key = self.http.post("/rest/mcp/api-key/rotate").raise_for_status().json()["data"]
            save(key_path, key)
        self.headers = {"Authorization": "Bearer " + key["apiKey"],
                        "Accept": "application/json, text/event-stream"}
        self.sequence = 0
        self.rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                "clientInfo": {"name": "line-test-migration", "version": "1.0"}})

    def rpc(self, method, params):
        self.sequence += 1
        r = self.http.post("/mcp-server/http", headers=self.headers,
                           json={"jsonrpc": "2.0", "id": self.sequence, "method": method, "params": params})
        r.raise_for_status()
        if r.headers.get("mcp-session-id"):
            self.headers["Mcp-Session-Id"] = r.headers["mcp-session-id"]
        if "text/event-stream" in r.headers.get("content-type", ""):
            messages = [json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: ")]
            value = next(m for m in messages if m.get("id") == self.sequence)
        else:
            value = r.json()
        if "error" in value:
            raise RuntimeError(value["error"])
        return value["result"]

    def call(self, name, arguments):
        result = self.rpc("tools/call", {"name": name, "arguments": arguments})
        if result.get("isError"):
            raise RuntimeError(json.dumps(result, ensure_ascii=False))
        return result


if __name__ == "__main__":
    client = LocalN8n()
    catalog = client.rpc("tools/list", {})
    save(LOGS / "mcp_tools.json", catalog)
    print(json.dumps([{"name": t["name"], "description": t.get("description", "")[:160]} for t in catalog["tools"]], ensure_ascii=False))
