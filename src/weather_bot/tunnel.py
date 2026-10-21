"""本機 Quick Tunnel 生命週期；公開驗證成功後才同步 LINE webhook。"""

import asyncio
import base64
from contextlib import suppress
from datetime import datetime, timezone
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess

import httpx

from weather_bot.config import PROJECT_ROOT

LOCAL_URL = "http://127.0.0.1:18567"
LINE_WEBHOOK = "https://api.line.me/v2/bot/channel/webhook"
URL_PATTERN = re.compile(r"https://[a-z0-9]+(?:-[a-z0-9]+)*\.trycloudflare\.com(?=[\s/|]|$)")
log = logging.getLogger(__name__)


class TunnelError(RuntimeError):
    pass


def error_summary(error):
    # 不記錄 API 回應本文、Authorization 或任意例外文字中的金鑰。
    if isinstance(error, TunnelError):
        return str(error)
    if isinstance(error, httpx.HTTPStatusError):
        return f"HTTP {error.response.status_code} ({error.request.url.host})"
    return type(error).__name__


class LocalTunnel:
    def __init__(self, settings, *, directory=None, executable=None):
        self.settings = settings
        self.directory = Path(directory or PROJECT_ROOT / "logs/service")
        self.executable = executable
        self.task = None
        self.process = None
        self.endpoint = None

    async def start(self):
        if not self.settings.line_token or not self.settings.line_secret:
            raise TunnelError("本機隧道需要 LINE_CHANNEL_ACCESS_TOKEN 與 LINE_CHANNEL_SECRET")
        portable = PROJECT_ROOT / ".tools/cloudflared.exe"
        self.executable = self.executable or (str(portable) if portable.is_file() else shutil.which("cloudflared"))
        if not self.executable:
            raise TunnelError("找不到 cloudflared；請先執行 scripts/install_cloudflared.ps1")
        self.directory.mkdir(parents=True, exist_ok=True)
        # 明確使用空設定，避免載入電腦上其他隧道的 config.yml。
        (self.directory / "cloudflared.yml").write_text("{}\n", encoding="utf-8")
        self._state("starting")
        self.task = asyncio.create_task(self._run(), name="local-tunnel")

    async def stop(self):
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
            self.task = None
            self._state("stopped")

    def _state(self, status, **details):
        state = {
            "status": status,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "service_pid": os.getpid(),
            "tunnel_pid": self.process.pid if self.process and self.process.returncode is None else None,
            "endpoint": self.endpoint,
            **details,
        }
        target = self.directory / "latest.json"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(target)

    async def _run(self):
        async with httpx.AsyncClient(timeout=10) as http:
            while True:
                try:
                    # 等 ASGI lifespan yield、socket 開始接收後才建立隧道。
                    await self._wait_health(http, LOCAL_URL)
                    await self._session(http)
                except Exception as error:
                    summary = error_summary(error)
                    log.error("本機隧道失敗，60 秒後重試：%s", summary)
                    self._state("retrying", error=summary)
                    await asyncio.sleep(60)

    async def _wait_health(self, http, base_url):
        async with asyncio.timeout(90):
            while True:
                try:
                    response = await http.get(base_url + "/health", timeout=5)
                    if response.status_code == 200 and response.json() == {"status": "ok"}:
                        return
                except (httpx.HTTPError, ValueError):
                    pass
                await asyncio.sleep(2)

    async def _read_output(self, stream, url):
        with (self.directory / "cloudflared.log").open("w", encoding="utf-8") as output:
            while line := await stream.readline():
                text = line.decode("utf-8", errors="replace")
                output.write(text)
                output.flush()
                match = URL_PATTERN.search(text)
                if match and not url.done():
                    url.set_result(match.group())
        if not url.done():
            url.set_exception(TunnelError("cloudflared 在產生公開網址前結束"))

    async def _session(self, http):
        self.endpoint = None
        self.process = await asyncio.create_subprocess_exec(
            self.executable, "tunnel", "--config", str(self.directory / "cloudflared.yml"),
            "--no-autoupdate", "--loglevel", "info", "--metrics", "127.0.0.1:0", "--url", LOCAL_URL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        self._state("connecting")
        url = asyncio.get_running_loop().create_future()
        reader = asyncio.create_task(self._read_output(self.process.stdout, url))
        try:
            base_url = await asyncio.wait_for(url, 60)
            self.endpoint = base_url + "/webhook"
            while self.process.returncode is None:
                try:
                    self._state("verifying")
                    await self._wait_health(http, base_url)
                    checks = await self._probe_webhook(http, base_url)
                    checks.update(await self._sync_webhook(http, self.endpoint))
                    self._state("ready", checks=checks)
                    log.info("LINE webhook 已同步並驗證：%s", self.endpoint)
                    break
                except Exception as error:
                    summary = error_summary(error)
                    self._state("retrying", error=summary)
                    log.error("隧道已建立，webhook 驗證／同步失敗，60 秒後重試：%s", summary)
                    await asyncio.sleep(60)
            code = await self.process.wait()
            raise TunnelError(f"cloudflared 已結束 (exit={code})")
        finally:
            if self.process.returncode is None:
                self.process.terminate()
                try:
                    await asyncio.wait_for(self.process.wait(), 5)
                except TimeoutError:
                    self.process.kill()
                    await self.process.wait()
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
            self.process = None

    async def _probe_webhook(self, http, base_url):
        body = b'{"events":[]}'
        signature = base64.b64encode(hmac.new(self.settings.line_secret.encode(), body, hashlib.sha256).digest()).decode()
        endpoint = base_url + "/webhook"
        valid = await http.post(endpoint, content=body, headers={"x-line-signature": signature})
        invalid = await http.post(endpoint, content=body, headers={"x-line-signature": "invalid"})
        if valid.status_code != 200 or invalid.status_code != 400:
            raise TunnelError(f"公開 webhook 驗簽失敗 (valid={valid.status_code}, invalid={invalid.status_code})")
        return {"public_health": 200, "valid_signature": 200, "invalid_signature": 400}

    async def _line(self, http, method, path, **kwargs):
        response = await http.request(method, LINE_WEBHOOK + path,
                                      headers={"Authorization": f"Bearer {self.settings.line_token}"}, **kwargs)
        response.raise_for_status()
        return response.json()

    async def _sync_webhook(self, http, endpoint):
        current = await self._line(http, "GET", "/endpoint")
        if current.get("active") is not True:
            raise TunnelError("請在 LINE Developers 開啟 Use webhook；API 無法開啟此選項")
        # 官方測試支援候選 URL；先讓 LINE 實際送簽章空事件，再改正式設定。
        tested = await self._line(http, "POST", "/test", json={"endpoint": endpoint})
        if tested.get("success") is not True or tested.get("statusCode") != 200:
            raise TunnelError(f"LINE webhook 測試失敗 (status={tested.get('statusCode')})")
        if current.get("endpoint") != endpoint:
            await self._line(http, "PUT", "/endpoint", json={"endpoint": endpoint})
        # LINE 設定可能快取一分鐘，確認回讀的新網址與啟用狀態。
        async with asyncio.timeout(70):
            while True:
                current = await self._line(http, "GET", "/endpoint")
                if current.get("endpoint") == endpoint and current.get("active") is True:
                    return {"line_test": True, "line_status": 200, "endpoint_matches": True, "active": True}
                await asyncio.sleep(2)
