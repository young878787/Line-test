"""沿用原服務的 Quick Tunnel 驗證／同步，改接僅公開 webhook 的 gateway。"""

import asyncio
import logging
import os
import subprocess

import httpx

from weather_bot.tunnel import LocalTunnel, TunnelError, error_summary
from runtime_config import GATEWAY_URL, RUNTIME

log = logging.getLogger(__name__)


class Pipe:
    def __init__(self, stream):
        self.stream = stream

    async def readline(self):
        return await asyncio.to_thread(self.stream.readline)


class WindowsProcess:
    def __init__(self, process):
        self.process = process
        self.pid = process.pid
        self.stdout = Pipe(process.stdout)

    @property
    def returncode(self):
        return self.process.poll()

    async def wait(self):
        return await asyncio.to_thread(self.process.wait)

    def terminate(self):
        self.process.terminate()

    def kill(self):
        self.process.kill()


async def spawn_cloudflared(*args):
    if os.name == "nt":
        process = await asyncio.to_thread(subprocess.Popen, args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                          creationflags=subprocess.CREATE_NO_WINDOW)
        return WindowsProcess(process)
    return await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)


class N8nTunnel(LocalTunnel):
    def __init__(self, settings, *, mock=False, directory=None, executable=None):
        super().__init__(settings, directory=directory or RUNTIME / "tunnel", executable=executable)
        self.mock = mock

    async def _run(self):
        async with httpx.AsyncClient(timeout=10, trust_env=False) as http:
            while True:
                try:
                    await self._wait_health(http, GATEWAY_URL)
                    await self._session(http)
                except Exception as error:
                    summary = error_summary(error)
                    self._state("retrying", error=summary)
                    log.error("HTTPS 隧道重試：%s", summary)
                    await asyncio.sleep(60)

    async def _session(self, http):
        self.endpoint = None
        self.process = await spawn_cloudflared(
            self.executable, "tunnel", "--config", str(self.directory / "cloudflared.yml"),
            "--no-autoupdate", "--loglevel", "info", "--metrics", "127.0.0.1:0", "--url", GATEWAY_URL)
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
                    if self.mock:
                        checks["line_sync"] = "NOT_RUN_MOCK"
                    else:
                        checks.update(await self._sync_webhook(http, self.endpoint))
                    self._state("ready", checks=checks)
                    break
                except Exception as error:
                    self._state("retrying", error=error_summary(error))
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
