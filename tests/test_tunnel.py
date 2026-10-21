import asyncio
import json
import httpx
import pytest

from weather_bot.config import Settings
from weather_bot.main import create_app
from weather_bot.tunnel import LINE_WEBHOOK, LocalTunnel, TunnelError, URL_PATTERN, error_summary


def test_only_accept_cloudflare_quick_tunnel_hostname():
    assert URL_PATTERN.search("| https://rain-bot-test.trycloudflare.com |\n").group() == "https://rain-bot-test.trycloudflare.com"
    for text in ("http://rain.trycloudflare.com", "https://rain.trycloudflare.com.evil.test", "https://evil.test"):
        assert URL_PATTERN.search(text) is None


async def test_public_probe_checks_real_signature_without_jobs_or_reply(bot, tmp_path):
    app = create_app(bot=bot)
    app.state.bot = bot
    tunnel = LocalTunnel(bot.settings, directory=tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as http:
        checks = await tunnel._probe_webhook(http, "http://test")
    assert checks == {"public_health": 200, "valid_signature": 200, "invalid_signature": 400}
    assert await bot.db.all("SELECT * FROM jobs") == []
    assert bot.line.replies == bot.line.pushes == bot.model.calls == []


async def test_restarted_url_is_tested_before_update_and_read_back(tmp_path, record_property):
    current = "https://old.example/webhook"
    calls = []
    settings = Settings(line_token="private-test-token", line_secret="private-test-secret")

    def handler(request):
        nonlocal current
        assert request.headers["authorization"] == "Bearer private-test-token"
        payload = json.loads(request.content) if request.content else None
        calls.append((request.method, request.url.path.rsplit("/", 1)[-1], payload))
        if request.method == "GET":
            return httpx.Response(200, json={"endpoint": current, "active": True})
        if request.method == "POST":
            assert payload["endpoint"] != current
            return httpx.Response(200, json={"success": True, "statusCode": 200})
        current = payload["endpoint"]
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        for name in ("first", "restarted"):
            tunnel = LocalTunnel(settings, directory=tmp_path)
            checks = await tunnel._sync_webhook(http, f"https://{name}.trycloudflare.com/webhook")
            assert checks["endpoint_matches"] and checks["active"] and checks["line_test"]
    assert current == "https://restarted.trycloudflare.com/webhook"
    assert [method for method, _, _ in calls] == ["GET", "POST", "PUT", "GET"] * 2
    record_property("restart_webhook_sync", "PASS: candidate tested before PUT, new endpoint read back")


@pytest.mark.parametrize("active,test_result,expected", [
    (False, {"success": True, "statusCode": 200}, "Use webhook"),
    (True, {"success": False, "statusCode": 400}, "status=400"),
    (True, {"success": True, "statusCode": 500}, "status=500"),
], ids=["inactive", "signature_rejected", "unsuccessful_status"])
async def test_failed_candidate_never_overwrites_webhook(tmp_path, active, test_result, expected):
    methods = []

    def handler(request):
        methods.append(request.method)
        if request.method == "GET":
            return httpx.Response(200, json={"endpoint": "https://old.example/webhook", "active": active})
        return httpx.Response(200, json=test_result)

    tunnel = LocalTunnel(Settings(line_token="fake"), directory=tmp_path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(TunnelError, match=expected):
            await tunnel._sync_webhook(http, "https://new.trycloudflare.com/webhook")
    assert "PUT" not in methods


async def test_sync_waits_for_line_cache(tmp_path, monkeypatch):
    endpoint = "https://new.trycloudflare.com/webhook"
    reads = 0
    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)

    def handler(request):
        nonlocal reads
        if request.method == "GET":
            reads += 1
            return httpx.Response(200, json={"endpoint": endpoint if reads == 3 else "https://old.example/webhook", "active": True})
        return httpx.Response(200, json={"success": True, "statusCode": 200})

    monkeypatch.setattr("weather_bot.tunnel.asyncio.sleep", sleep)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await LocalTunnel(Settings(line_token="fake"), directory=tmp_path)._sync_webhook(http, endpoint)
    assert result["endpoint_matches"] and reads == 3 and sleeps == [2]


async def test_process_exit_restarts_tunnel_resyncs_and_shutdown_cleans_up(tmp_path, monkeypatch, record_property):
    processes, synced = [], []
    real_sleep = asyncio.sleep

    class Process:
        def __init__(self, number):
            self.pid, self.returncode = 100 + number, None
            self.done = asyncio.Event()
            self.stdout = asyncio.StreamReader()
            self.stdout.feed_data(f"| https://url-{number}.trycloudflare.com |\n".encode())

        async def wait(self):
            await self.done.wait()
            return self.returncode

        def terminate(self):
            self.returncode = 1
            self.done.set()
            self.stdout.feed_eof()

        kill = terminate

    async def spawn(*args, **kwargs):
        assert args[-1] == "http://127.0.0.1:18567" and "--no-autoupdate" in args
        process = Process(len(processes) + 1)
        processes.append(process)
        return process

    async def health(*args):
        pass

    async def probe(*args):
        return {"valid_signature": 200}

    async def sync(http, endpoint):
        synced.append(endpoint)
        if len(synced) == 1:
            processes[0].terminate()
        return {"endpoint_matches": True, "line_test": True}

    async def sleep(seconds):
        await real_sleep(0)

    monkeypatch.setattr("weather_bot.tunnel.asyncio.create_subprocess_exec", spawn)
    monkeypatch.setattr("weather_bot.tunnel.asyncio.sleep", sleep)
    tunnel = LocalTunnel(Settings(line_token="private-token", line_secret="private-secret"),
                         directory=tmp_path, executable="fake-cloudflared")
    monkeypatch.setattr(tunnel, "_wait_health", health)
    monkeypatch.setattr(tunnel, "_probe_webhook", probe)
    monkeypatch.setattr(tunnel, "_sync_webhook", sync)
    await tunnel.start()
    try:
        async with asyncio.timeout(3):
            while len(synced) < 2:
                await real_sleep(0.01)
        state = json.loads((tmp_path / "latest.json").read_text(encoding="utf-8"))
        assert state["status"] == "ready" and state["tunnel_pid"] == 102
        assert synced == [f"https://url-{n}.trycloudflare.com/webhook" for n in (1, 2)]
    finally:
        await tunnel.stop()
    assert all(p.returncode is not None for p in processes)
    assert json.loads((tmp_path / "latest.json").read_text(encoding="utf-8"))["status"] == "stopped"
    assert "private-" not in (tmp_path / "latest.json").read_text(encoding="utf-8")
    record_property("tunnel_recovery", "PASS: process restart, new URL sync, child cleanup")


async def test_missing_credentials_fail_before_child_process(tmp_path):
    tunnel = LocalTunnel(Settings(), directory=tmp_path, executable="unused")
    with pytest.raises(TunnelError, match="LINE_CHANNEL_ACCESS_TOKEN"):
        await tunnel.start()
    assert tunnel.task is None and tunnel.process is None


def test_http_error_summary_does_not_disclose_response_or_credentials():
    request = httpx.Request("PUT", LINE_WEBHOOK + "/endpoint", headers={"Authorization": "Bearer private-token"})
    response = httpx.Response(401, request=request, json={"message": "private-token"})
    error = httpx.HTTPStatusError("private-token", request=request, response=response)
    assert error_summary(error) == "HTTP 401 (api.line.me)"


async def test_asgi_lifespan_stops_tunnel_before_bot(monkeypatch):
    calls = []

    class Runtime:
        settings = Settings(line_secret="test")

        async def start(self):
            calls.append("bot-start")

        async def stop(self):
            calls.append("bot-stop")

    class Tunnel:
        def __init__(self, settings):
            pass

        async def start(self):
            calls.append("tunnel-start")

        async def stop(self):
            calls.append("tunnel-stop")

    monkeypatch.setattr("weather_bot.main.LocalTunnel", Tunnel)
    app = create_app(bot=Runtime(), local_tunnel=True)
    async with app.router.lifespan_context(app):
        assert calls == ["bot-start", "tunnel-start"]
    assert calls == ["bot-start", "tunnel-start", "tunnel-stop", "bot-stop"]
