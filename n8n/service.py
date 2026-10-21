"""單一常駐入口：n8n、業務 adapter、公開 gateway、動態 HTTPS 與 LINE 同步。"""

import argparse
import asyncio
from contextlib import contextmanager, suppress
from datetime import datetime, timezone
import json
import logging
import os
import signal
import sys

import httpx
import uvicorn

from runtime_config import ADAPTER_URL, GATEWAY_URL, N8N_URL, ROOT, RUNTIME, internal_key, load_settings
from adapter.app import create_apps
from processes import NodeRuntime, N8nStartupError, ServiceLock, bind_listener, port_open
from tunnel import N8nTunnel

sys.path.insert(0, str(ROOT / "scripts"))
from mcp_client import LocalN8n
from build_workflows import build, unpack

log = logging.getLogger("line_n8n")


class ManagedServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self):
        yield


class Service:
    def __init__(self, *, mock=False):
        self.mock = mock
        self.settings = load_settings(mock)
        self.stop_event = asyncio.Event()
        self.node = NodeRuntime()
        self.mcp = None
        self.manifest = None
        self.tunnel = None
        self.servers, self.server_tasks, self.sockets = [], [], []
        self.local_ready = False
        self.n8n_restarts = 0
        self.status = "starting"
        self.failure = {}

    def state(self, status=None, **extra):
        previous = self.status
        if status:
            self.status = status
        if "error" in extra:
            self.failure = {k: extra[k] for k in ("error", "error_detail") if k in extra}
        elif self.status not in {"failed", "retrying"}:
            self.failure = {}
        state = {"status": self.status, "checked_at": datetime.now(timezone.utc).isoformat(),
                 "mode": "mock" if self.mock else "live", "service_pid": os.getpid(), "n8n_pid": self.node.pid,
                 "n8n_url": N8N_URL, "adapter_url": ADAPTER_URL, "gateway_url": GATEWAY_URL,
                 "workflow_id": self.manifest["id"] if self.manifest else None,
                 "database_path": self.settings.database, "n8n_restarts": self.n8n_restarts,
                 "endpoint": None, "https": "NOT_RUN_MOCK" if self.mock else "starting", **self.failure, **extra}
        if self.tunnel:
            path = self.tunnel.directory / "latest.json"
            if path.exists():
                tunnel = json.loads(path.read_text(encoding="utf-8"))
                state.update(endpoint=tunnel.get("endpoint"), https=tunnel["status"], checks=tunnel.get("checks", {}))
        target = RUNTIME / "service.json"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(target)
        if self.status != previous:
            log.info("服務狀態：%s", self.status)

    def startup_progress(self, elapsed, probe):
        self.state(startup_elapsed_seconds=elapsed, startup_probe=probe)

    async def wait_adapter(self):
        async with httpx.AsyncClient(timeout=2, trust_env=False) as http:
            async with asyncio.timeout(20):
                while True:
                    for task in self.server_tasks:
                        if task.done():
                            task.result()
                            raise RuntimeError("Adapter 啟動失敗")
                    try:
                        if (await http.get(ADAPTER_URL + "/health")).status_code == 200:
                            return
                    except httpx.HTTPError:
                        pass
                    await asyncio.sleep(.1)

    async def configure(self):
        def configure_sync():
            self.mcp = LocalN8n()
            manifest = build(mock=self.mock, client=self.mcp)
            self.mcp.call("publish_workflow", {"workflowId": manifest["id"]})
            detail = unpack(self.mcp.call("get_workflow_details", {"workflowId": manifest["id"]}))["workflow"]
            if not detail["active"]:
                raise RuntimeError("Workflow 未啟用")
            return manifest
        configuration = asyncio.create_task(asyncio.to_thread(configure_sync))
        try:
            self.manifest = await asyncio.shield(configuration)
        except asyncio.CancelledError:
            # to_thread 無法取消已開始的 MCP 呼叫；先等它完成，再停用同一個流程。
            self.manifest = await configuration
            raise
        self.local_ready = True

    async def recover_n8n(self):
        self.local_ready = False
        self.state("restarting_n8n")
        if self.tunnel:
            await self.tunnel.stop()
            self.tunnel = None
        if self.mcp:
            self.mcp.http.close()
            self.mcp = None
        await self.node.stop()
        await self.node.ensure(progress=self.startup_progress)
        await self.configure()
        self.n8n_restarts += 1
        if not self.mock:
            self.tunnel = N8nTunnel(self.settings)
            await self.tunnel.start()
        self.state("ready" if self.mock else "verifying_https")

    async def run(self):
        # 原服務與新服務不可同時領取同一份 LINE 工作／正式 SQLite。
        if not self.mock and port_open(18567):
            raise RuntimeError("原 Python 服務仍在 18567 運行；請先停止原服務，再啟動此替代入口")
        self.sockets = [bind_listener(5682)]
        try:
            self.sockets.append(bind_listener(5683))
            self.state("starting_n8n")
            await self.node.ensure(progress=self.startup_progress)
            log.info("啟動 adapter %s 與公開 gateway %s", ADAPTER_URL, GATEWAY_URL)
            private, public = create_apps(self.settings, mock=self.mock, key=internal_key(),
                                          ready=lambda: self.local_ready, request_stop=self.stop_event.set)
            for app, sock in zip((private, public), self.sockets):
                server = ManagedServer(uvicorn.Config(app, log_level="warning", access_log=False,
                                                     timeout_keep_alive=30, timeout_graceful_shutdown=15))
                self.servers.append(server)
                self.server_tasks.append(asyncio.create_task(server.serve(sockets=[sock])))
            await self.wait_adapter()
            self.state("configuring_workflow")
            log.info("透過 MCP 驗證及發布單一 workflow，請稍候")
            await self.configure()
            if not self.mock:
                self.tunnel = N8nTunnel(self.settings)
                await self.tunnel.start()
            self.state("ready" if self.mock else "verifying_https")
            print(f"LINE n8n {'mock' if self.mock else 'live'} 已啟動：{N8N_URL}；Ctrl+C 停止。", flush=True)
            failures = 0
            async with httpx.AsyncClient(timeout=2, trust_env=False) as http:
                while not self.stop_event.is_set():
                    for task in self.server_tasks:
                        if task.done():
                            task.result()
                            raise RuntimeError("Adapter 已結束")
                    try:
                        healthy = (await http.get(N8N_URL + "/healthz")).status_code == 200
                    except httpx.HTTPError:
                        healthy = False
                    failures = 0 if healthy else failures + 1
                    if failures >= 2:
                        try:
                            await self.recover_n8n()
                            failures = 0
                        except Exception as error:
                            self.state("retrying", error=type(error).__name__)
                            log.error("n8n 恢復失敗：%s", type(error).__name__)
                    else:
                        tunnel_ready = self.mock
                        if self.tunnel:
                            path = self.tunnel.directory / "latest.json"
                            if path.exists():
                                tunnel_ready = json.loads(path.read_text(encoding="utf-8"))["status"] == "ready"
                        self.state("ready" if tunnel_ready else "verifying_https")
                    with suppress(TimeoutError):
                        await asyncio.wait_for(self.stop_event.wait(), 1)
        except asyncio.CancelledError:
            if not self.stop_event.is_set():
                self.state("failed", error="CancelledError")
                raise
        except BaseException as error:
            details = {"error_detail": str(error)} if isinstance(error, N8nStartupError) else {}
            self.state("failed", error=type(error).__name__, **details)
            raise
        finally:
            cleanup = asyncio.create_task(self.close())
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                # 停止訊號也可能剛好落在 finally；不可中斷子程序與 listener 清理。
                await cleanup
                raise

    async def close(self):
        failed = self.status == "failed"
        self.local_ready = False
        if not failed:
            self.state("stopping")
        if self.tunnel:
            await self.tunnel.stop()
        if self.mcp and self.manifest:
            with suppress(Exception):
                await asyncio.to_thread(self.mcp.call, "unpublish_workflow", {"workflowId": self.manifest["id"]})
        # 先停公開入口，再停止業務 API，讓 in-flight request 有機會完成。
        for server, task in reversed(list(zip(self.servers, self.server_tasks))):
            server.should_exit = True
            with suppress(Exception, asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(task), 20)
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        for sock in self.sockets:
            sock.close()
        with suppress(Exception):
            await self.node.stop()
        if self.mcp:
            self.mcp.http.close()
        if failed:
            self.state("failed")
        else:
            self.state("stopped")
        log.info("服務清理完成：%s", self.status)


async def run(mock=False):
    service = Service(mock=mock)
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: loop.call_soon_threadsafe(service.stop_event.set))
    running = asyncio.create_task(service.run())
    stopping = asyncio.create_task(service.stop_event.wait())
    try:
        await asyncio.wait((running, stopping), return_when=asyncio.FIRST_COMPLETED)
        if stopping.done() and not running.done():
            running.cancel()
        try:
            await running
        except asyncio.CancelledError:
            if not service.stop_event.is_set():
                raise
    finally:
        stopping.cancel()
        await asyncio.gather(stopping, return_exceptions=True)


def main():
    parser = argparse.ArgumentParser(description="LINE Weather 單一 n8n workflow 常駐服務")
    parser.add_argument("--mock", action="store_true", help="隔離資料庫、固定時鐘、HTTP mock，不建立公開隧道或同步 LINE")
    args = parser.parse_args()
    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    log.info("啟動 LINE n8n %s；n8n 初始化期間每 5 秒回報進度，Ctrl+C 可停止", "mock" if args.mock else "live")
    try:
        with ServiceLock():
            asyncio.run(run(args.mock))
    except (RuntimeError, ValueError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
