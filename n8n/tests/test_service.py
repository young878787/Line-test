"""常駐模式、公開路由、來源期限及隧道生命週期的隔離驗證。"""

import asyncio
import base64
from contextlib import suppress
import hashlib
import hmac
import json
import os
from pathlib import Path
import sys
import threading
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from adapter.app import create_apps, receipt_proof
from runtime_config import INTERNAL_HEADER, RUNTIME, load_settings
from service import Service
from processes import NodeRuntime, N8nStartupError
from tunnel import N8nTunnel

KEY = "unit-only-internal-key"


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.settings = load_settings(True)
        self.settings.database = ":memory:"
        self.private, self.public = create_apps(self.settings, mock=True, key=KEY)
        self.context = self.private.router.lifespan_context(self.private)
        await self.context.__aenter__()
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.private), base_url="http://unit",
                                      headers={INTERNAL_HEADER: KEY})

    async def asyncTearDown(self):
        await self.http.aclose()
        await self.context.__aexit__(None, None, None)

    async def test_public_surface_excludes_admin_and_mock_routes(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.public), base_url="http://public") as http:
            self.assertEqual((await http.get("/health")).json(), {"status": "ok"})
            for path in ("/rest/settings", "/mcp-server/http", "/ingress", "/mock/reset", "/service/stop", "/openapi.json"):
                self.assertEqual((await http.post(path)).status_code, 404)

    async def test_private_routes_require_credential(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.private), base_url="http://unit") as http:
            for path in ("/ingress", "/jobs/chats", "/jobs/background", "/mock/reset", "/service/stop"):
                self.assertEqual((await http.post(path)).status_code, 401)

    async def test_gateway_preserves_bytes_and_authenticates_receipt(self):
        captured = []
        body = '{ "events": [], "text": "保留原始空白" }'.encode()

        def handler(request):
            captured.append(request)
            return httpx.Response(200, json={"status": "ok"})

        original = self.private.state.n8n_http
        self.private.state.n8n_http = httpx.AsyncClient(base_url="http://n8n", transport=httpx.MockTransport(handler))
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.public), base_url="http://public") as public:
                self.assertEqual((await public.post("/webhook", content=body)).status_code, 200)
            request = captured[0]
            self.assertEqual(request.content, body)
            self.assertEqual(request.headers["x-received-proof"], receipt_proof(KEY, request.headers["x-received-at"], body))
            self.assertNotIn(INTERNAL_HEADER, request.headers)
        finally:
            await self.private.state.n8n_http.aclose()
            self.private.state.n8n_http = original

    async def test_forged_receipt_does_not_persist_event(self):
        body = b'{"events":[]}'
        signature = base64.b64encode(hmac.new(self.settings.line_secret.encode(), body, hashlib.sha256).digest()).decode()
        response = await self.http.post("/ingress", content=body, headers={"x-line-signature": signature,
                                       "x-received-at": "999999999999", "x-received-proof": "forged"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(await self.private.state.bot.db.all("SELECT * FROM jobs"), [])

    async def test_gateway_receipt_deadline_covers_queue_before_adapter(self):
        bot = self.private.state.bot
        body = json.dumps({"events": [{"type": "message", "webhookEventId": "aged", "replyToken": "unit-token",
                    "source": {"type": "user", "userId": "Uaged"}, "message": {"type": "text", "text": "你好"}}]}).encode()
        stamp = str(bot.clock.now().timestamp() - 56)
        signature = base64.b64encode(hmac.new(self.settings.line_secret.encode(), body, hashlib.sha256).digest()).decode()
        response = await self.http.post("/ingress", content=body, headers={"x-line-signature": signature,
                                       "x-received-at": stamp, "x-received-proof": receipt_proof(KEY, stamp, body)})
        self.assertEqual(response.status_code, 200)
        await self.http.post("/jobs/chats")
        self.assertEqual((await bot.db.one("SELECT status FROM jobs"))["status"], "expired")
        self.assertEqual(self.private.state.services.replies, [])

    async def test_live_factory_has_real_clock_and_no_mock_routes(self):
        private, _ = create_apps(self.settings, mock=False, key=KEY)
        async with private.router.lifespan_context(private):
            self.assertIsNone(private.state.services)
            self.assertNotEqual(type(private.state.bot.clock).__name__, "MockClock")
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=private), base_url="http://unit", headers={INTERNAL_HEADER: KEY}) as http:
                self.assertEqual((await http.post("/mock/reset")).status_code, 404)
                self.assertEqual((await http.get("/mock/evidence")).status_code, 404)

    async def test_live_guard_refuses_original_service_before_n8n(self):
        with patch("service.load_settings", return_value=self.settings), patch("service.port_open", return_value=True):
            service = Service(mock=False)
            with patch.object(service.node, "ensure", new_callable=AsyncMock) as start:
                with self.assertRaisesRegex(RuntimeError, "18567"):
                    await service.run()
                start.assert_not_called()


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_startup_failure_closes_sockets_and_preserves_reason(self):
        service = Service(mock=True)
        sockets = [Mock(), Mock()]
        with TemporaryDirectory() as directory, patch("service.RUNTIME", Path(directory)), \
             patch("service.bind_listener", side_effect=sockets), \
             patch.object(service.node, "ensure", new_callable=AsyncMock,
                          side_effect=N8nStartupError("n8n did not become ready; see n8n.log")), \
             patch.object(service.node, "stop", new_callable=AsyncMock) as stop:
            with self.assertRaises(N8nStartupError):
                await service.run()
            state = json.loads((Path(directory) / "service.json").read_text())
            self.assertEqual(state["status"], "failed")
            self.assertEqual(state["error"], "N8nStartupError")
            self.assertIn("n8n.log", state["error_detail"])
            stop.assert_awaited_once()
            for sock in sockets:
                sock.close.assert_called_once()

    async def test_cancel_during_cleanup_waits_until_children_are_reaped(self):
        service = Service(mock=True)
        entered, release, completed = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def cleanup():
            entered.set()
            await release.wait()
            completed.set()

        with patch("service.bind_listener", return_value=Mock()), patch.object(service, "state"), \
             patch.object(service.node, "ensure", new_callable=AsyncMock, side_effect=RuntimeError("startup failed")), \
             patch.object(service, "close", side_effect=cleanup):
            running = asyncio.create_task(service.run())
            await asyncio.wait_for(entered.wait(), 2)
            running.cancel()
            await asyncio.sleep(0)
            self.assertFalse(running.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await running
        self.assertTrue(completed.is_set())

    async def test_cancel_during_mcp_configuration_finishes_before_cleanup(self):
        service = Service(mock=True)
        entered, release = threading.Event(), threading.Event()
        manifest = {"id": "unit-workflow"}

        def build(**kwargs):
            entered.set()
            if not release.wait(3):
                raise TimeoutError("unit configuration was not released")
            return manifest

        client = Mock()
        client.call.return_value = {"structuredContent": {"workflow": {"active": True}}}
        with patch("service.LocalN8n", return_value=client), patch("service.build", side_effect=build):
            configuring = asyncio.create_task(service.configure())
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            configuring.cancel()
            await asyncio.sleep(0)
            self.assertFalse(configuring.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await configuring
        self.assertEqual(service.manifest, manifest)
        self.assertFalse(service.local_ready)


class StartupTests(unittest.IsolatedAsyncioTestCase):
    def http(self, handler):
        client = Mock()
        client.get = AsyncMock(side_effect=handler)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        return client

    async def test_slow_start_beyond_old_deadline_can_become_ready(self):
        clock, reports = {"now": 0, "calls": 0}, []

        async def get(*args, **kwargs):
            clock["calls"] += 1
            if clock["calls"] == 1:
                clock["now"] = 121
                return httpx.Response(503)
            return httpx.Response(200)

        client = self.http(get)
        node = NodeRuntime()
        node.pid = 123
        with patch("processes.monotonic", side_effect=lambda: clock["now"]), \
             patch("processes.httpx.AsyncClient", return_value=client), \
             patch("processes.asyncio.sleep", new_callable=AsyncMock):
            await node._wait_ready(lambda elapsed, probe: reports.append((elapsed, probe)))
        self.assertEqual(client.get.await_count, 2)
        self.assertEqual(reports[-1], (121, "HTTP 200"))

    async def test_deadline_reports_last_probe_without_cancelled_traceback(self):
        clock = {"now": 0}

        async def get(*args, **kwargs):
            clock["now"] = 2
            return httpx.Response(503)

        node = NodeRuntime()
        node.pid = 123
        with patch("processes.STARTUP_TIMEOUT", 1), patch("processes.monotonic", side_effect=lambda: clock["now"]), \
             patch("processes.httpx.AsyncClient", return_value=self.http(get)), \
             patch("processes.asyncio.sleep", new_callable=AsyncMock):
            with self.assertRaises(N8nStartupError) as caught:
                await node._wait_ready()
        self.assertIn("HTTP 503", str(caught.exception))
        self.assertIn("n8n.log", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)

    async def test_early_child_exit_fails_without_waiting_for_deadline(self):
        node = NodeRuntime()
        node.process = Mock(returncode=7)
        node.process.poll.return_value = 7
        client = self.http(None)
        with patch("processes.httpx.AsyncClient", return_value=client):
            with self.assertRaisesRegex(N8nStartupError, "exit=7"):
                await node._wait_ready()
        client.get.assert_not_awaited()

    async def test_transient_http_failure_keeps_progress_and_retries(self):
        clock, reports = {"now": 0, "calls": 0}, []

        async def get(*args, **kwargs):
            clock["calls"] += 1
            if clock["calls"] == 1:
                clock["now"] = 6
                raise httpx.ReadTimeout("unit-only timeout")
            return httpx.Response(200)

        with patch("processes.monotonic", side_effect=lambda: clock["now"]), \
             patch("processes.httpx.AsyncClient", return_value=self.http(get)), \
             patch("processes.asyncio.sleep", new_callable=AsyncMock):
            await NodeRuntime()._wait_ready(lambda elapsed, probe: reports.append((elapsed, probe)))
        self.assertIn((6, "ReadTimeout"), reports)
        self.assertEqual(reports[-1], (6, "HTTP 200"))


class TunnelTests(unittest.IsolatedAsyncioTestCase):
    async def test_candidate_verification_precedes_update_on_every_new_url(self):
        current, methods = "https://old.example/webhook", []

        def handler(request):
            nonlocal current
            methods.append(request.method)
            if request.method == "GET":
                return httpx.Response(200, json={"endpoint": current, "active": True})
            if request.method == "POST":
                return httpx.Response(200, json={"success": True, "statusCode": 200})
            current = json.loads(request.content)["endpoint"]
            return httpx.Response(200, json={})

        tunnel = N8nTunnel(load_settings(True))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            for name in ("first", "restarted"):
                checks = await tunnel._sync_webhook(http, f"https://{name}.trycloudflare.com/webhook")
                self.assertTrue(checks["endpoint_matches"])
        self.assertEqual(methods, ["GET", "POST", "PUT", "GET"] * 2)
        self.assertIn("restarted", current)

    async def test_failed_candidate_never_overwrites_endpoint(self):
        methods = []

        def handler(request):
            methods.append(request.method)
            return httpx.Response(200, json={"endpoint": "https://old.example/webhook", "active": True} if request.method == "GET" else {"success": False, "statusCode": 400})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            with self.assertRaises(RuntimeError):
                await N8nTunnel(load_settings(True))._sync_webhook(http, "https://new.trycloudflare.com/webhook")
        self.assertNotIn("PUT", methods)

    async def test_tunnel_exit_gets_new_url_and_stop_reaps_children(self):
        processes, synced = [], []
        real_sleep = asyncio.sleep

        class Process:
            def __init__(self, number):
                self.pid, self.returncode = number, None
                self.done = asyncio.Event()
                self.stdout = asyncio.StreamReader()
                self.stdout.feed_data(f"https://url-{number}.trycloudflare.com\n".encode())

            async def wait(self):
                await self.done.wait()
                return self.returncode

            def terminate(self):
                self.returncode = 1
                self.done.set()
                self.stdout.feed_eof()

            kill = terminate

        async def spawn(*args):
            self.assertEqual(args[-1], "http://127.0.0.1:5683")
            process = Process(len(processes) + 1)
            processes.append(process)
            return process

        async def sync(http, endpoint):
            synced.append(endpoint)
            if len(synced) == 1:
                processes[0].terminate()
            return {"endpoint_matches": True}

        async def sleep(seconds):
            await real_sleep(0)

        tunnel = N8nTunnel(load_settings(True), directory=RUNTIME / "unit_tunnel", executable="fake-cloudflared")
        with patch("tunnel.spawn_cloudflared", side_effect=spawn), patch("tunnel.asyncio.sleep", side_effect=sleep), \
             patch.object(tunnel, "_wait_health", new_callable=AsyncMock), \
             patch.object(tunnel, "_probe_webhook", new_callable=AsyncMock, return_value={"valid_signature": 200}), \
             patch.object(tunnel, "_sync_webhook", side_effect=sync):
            await tunnel.start()
            try:
                async with asyncio.timeout(3):
                    while len(synced) < 2:
                        await real_sleep(.01)
            finally:
                await tunnel.stop()
        self.assertEqual(synced, [f"https://url-{n}.trycloudflare.com/webhook" for n in (1, 2)])
        self.assertTrue(all(p.returncode is not None for p in processes))
        self.assertEqual(json.loads((tunnel.directory / "latest.json").read_text())["status"], "stopped")


if __name__ == "__main__":
    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(stream=sys.stdout, verbosity=2).run(suite)
    report = {"status": "passed" if result.wasSuccessful() else "failed", "total": result.testsRun,
              "passed": result.testsRun - len(result.failures) - len(result.errors), "failed": len(result.failures) + len(result.errors),
              "failures": [{"id": str(case), "error": error} for case, error in result.failures + result.errors]}
    (ROOT / "logs/service_tests.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    sys.exit(0 if result.wasSuccessful() else 1)
