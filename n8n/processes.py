"""只管理本專案的 n8n 子程序，並獨占服務入口。"""

import asyncio
import json
import logging
import os
from pathlib import Path
import socket
import subprocess
from time import monotonic

import httpx

from runtime_config import N8N_URL, ROOT, RUNTIME

STARTUP_TIMEOUT = 300
log = logging.getLogger("line_n8n.processes")


class N8nStartupError(RuntimeError):
    """可直接顯示的啟動錯誤，不包含環境值或 API 回應。"""


def port_open(port):
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name == "nt":
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        probe.bind(("127.0.0.1", port))
        return False
    except OSError:
        return True
    finally:
        probe.close()


def listener(port):
    if os.name != "nt":
        raise RuntimeError("目前啟動器支援此專案的 Windows 環境")
    code = (f"$c=Get-NetTCPConnection -LocalPort {int(port)} -State Listen -ErrorAction SilentlyContinue;"
            "if($c){Get-CimInstance Win32_Process -Filter ('ProcessId='+$c[0].OwningProcess) | Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress}")
    output = subprocess.run(["powershell", "-NoProfile", "-Command", code], capture_output=True,
                            encoding="utf-8", errors="replace", creationflags=subprocess.CREATE_NO_WINDOW, check=True).stdout.strip()
    return json.loads(output) if output else None


def bind_listener(port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name == "nt":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        sock.bind(("127.0.0.1", port))
        sock.listen(128)
        sock.setblocking(False)
        return sock
    except BaseException:
        sock.close()
        raise


class ServiceLock:
    def __enter__(self):
        import msvcrt
        RUNTIME.mkdir(parents=True, exist_ok=True)
        self.file = (RUNTIME / "service.lock").open("a+b")
        self.file.seek(0)
        if not self.file.read(1):
            self.file.write(b"1")
            self.file.flush()
        self.file.seek(0)
        try:
            msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            self.file.close()
            raise RuntimeError("n8n 服務已啟動；請查看 n8n/.runtime/service.json") from None
        return self

    def __exit__(self, *args):
        import msvcrt
        self.file.seek(0)
        msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
        self.file.close()


class NodeRuntime:
    def __init__(self):
        self.pid = None
        self.process = None
        self.output = None

    def verify_owner(self):
        state_file = RUNTIME / "instance.json"
        state = json.loads(state_file.read_text(encoding="utf-8-sig")) if state_file.exists() else {}
        actual = listener(5680)
        command = (actual or {}).get("CommandLine", "").replace("\\", "/")
        if not actual or actual["ProcessId"] != state.get("pid") or "/node_modules/n8n/bin/n8n" not in command:
            raise RuntimeError("5680 不屬於本專案記錄的 n8n 實例，拒絕操作")
        return actual["ProcessId"]

    async def ensure(self, progress=None):
        if port_open(5680):
            self.pid = await asyncio.to_thread(self.verify_owner)
        else:
            if port_open(5681):
                raise RuntimeError("5681 已被其他服務使用")
            package = Path(os.environ["APPDATA"]) / "npm/node_modules/n8n"
            if not (package / "bin/n8n").is_file():
                raise RuntimeError("找不到 n8n；請先安裝 n8n@2.35.7")
            version = json.loads((package / "package.json").read_text(encoding="utf-8"))["version"]
            if version != "2.35.7":
                raise RuntimeError("此專案驗證的 n8n 版本為 2.35.7")
            env = {**os.environ, "N8N_USER_FOLDER": str(RUNTIME), "N8N_HOST": "127.0.0.1", "N8N_LISTEN_ADDRESS": "127.0.0.1",
                   "N8N_PORT": "5680", "N8N_RUNNERS_BROKER_PORT": "5681", "GENERIC_TIMEZONE": "Asia/Taipei",
                   "N8N_EDITOR_BASE_URL": N8N_URL, "N8N_WEBHOOK_URL": N8N_URL + "/", "N8N_SECURE_COOKIE": "false",
                   "N8N_DIAGNOSTICS_ENABLED": "false", "N8N_VERSION_NOTIFICATIONS_ENABLED": "false",
                   "N8N_TEMPLATES_ENABLED": "false", "N8N_LOG_LEVEL": "info"}
            env.pop("WEBHOOK_URL", None)
            self.output = (RUNTIME / "n8n.log").open("w", encoding="utf-8")
            self.process = subprocess.Popen(["node", str(package / "bin/n8n"), "start"], cwd=ROOT.parent, env=env,
                                            stdout=self.output, stderr=subprocess.STDOUT,
                                            creationflags=subprocess.CREATE_NO_WINDOW)
            self.pid = self.process.pid
            (RUNTIME / "instance.json").write_text(json.dumps({"pid": self.pid, "url": N8N_URL}), encoding="utf-8")
        log.info("n8n 程序 PID=%s；編輯網址 %s；啟動日誌 %s", self.pid, N8N_URL, RUNTIME / "n8n.log")
        await self._wait_ready(progress)

    async def _wait_ready(self, progress=None):
        started = monotonic()
        next_report = 0
        last_probe = "尚未連線"
        async with httpx.AsyncClient(timeout=2, trust_env=False) as http:
            try:
                async with asyncio.timeout(STARTUP_TIMEOUT):
                    while monotonic() - started < STARTUP_TIMEOUT:
                        elapsed = monotonic() - started
                        if self.process and self.process.poll() is not None:
                            raise N8nStartupError(f"n8n 子程序提前結束 (exit={self.process.returncode})；請檢查 {RUNTIME / 'n8n.log'}")
                        if elapsed >= next_report:
                            log.info("等待 n8n 初始化：%.0f/%s 秒，PID=%s，探測=%s", elapsed, STARTUP_TIMEOUT, self.pid, last_probe)
                            if progress:
                                progress(round(elapsed, 1), last_probe)
                            next_report = elapsed + 5
                        try:
                            response = await http.get(N8N_URL + "/rest/settings", timeout=min(2, STARTUP_TIMEOUT - elapsed))
                            last_probe = f"HTTP {response.status_code}"
                            if response.status_code == 200:
                                elapsed = round(monotonic() - started, 1)
                                if progress:
                                    progress(elapsed, last_probe)
                                log.info("n8n 就緒：%s，初始化耗時 %.1f 秒", N8N_URL, elapsed)
                                return
                        except httpx.HTTPError as error:
                            last_probe = type(error).__name__
                        await asyncio.sleep(.5)
            except TimeoutError:
                pass
        raise N8nStartupError(f"n8n 在 {STARTUP_TIMEOUT} 秒內未就緒 (PID={self.pid}，最後探測={last_probe})；請檢查 {RUNTIME / 'n8n.log'}")

    async def stop(self):
        if self.pid is None:
            return
        if port_open(5680):
            if await asyncio.to_thread(self.verify_owner) != self.pid:
                raise RuntimeError("n8n PID 已改變，拒絕停止其他程序")
        elif self.process is None:
            self.pid = None
            return
        if self.process and self.process.poll() is not None:
            self.pid = None
        else:
            await asyncio.to_thread(subprocess.run, ["taskkill", "/PID", str(self.pid), "/T", "/F"],
                                    capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
            if self.process:
                await asyncio.to_thread(self.process.wait, 10)
            self.pid = None
        if self.output:
            self.output.close()
            self.output = None
