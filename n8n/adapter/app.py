"""單一 workflow 的業務 adapter，以及僅公開 health/webhook 的 gateway。"""

import asyncio
from contextlib import asynccontextmanager
import hashlib
import hmac
import json
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
import httpx
from pydantic import BaseModel, ConfigDict, Field

from weather_bot.config import Clock
from weather_bot.line_io import verify_signature
from weather_bot.workers import Bot
from runtime_config import INTERNAL_HEADER, N8N_URL, RUNTIME, WEBHOOK_PATH
from .mock_services import MockClock, MockServices


class Control(BaseModel):
    model_config = ConfigDict(extra="forbid")
    advance_seconds: float = Field(default=0, ge=0, le=86400 * 2)
    ai_failure: bool | None = None
    cwa_failure: bool | None = None
    weather_rain: bool | None = None
    reply_failure: bool | None = None
    ai_delay: float | None = Field(default=None, ge=0, le=5)
    memory_delay: float | None = Field(default=None, ge=0, le=5)
    push_failures: int | None = Field(default=None, ge=0, le=3)


async def raw_body(request):
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 1024 * 1024:
            raise HTTPException(413, "Webhook body too large")
    return bytes(body)


def receipt_proof(key, received_at, body):
    return hmac.new(key.encode(), received_at.encode() + b"\n" + body, hashlib.sha256).hexdigest()


def create_apps(settings, *, mock, key, ready=lambda: True, request_stop=None):
    clock = MockClock() if mock else Clock()
    services = MockServices(clock) if mock else None
    dispatch_lock, background_lock = asyncio.Lock(), asyncio.Lock()
    errors = []

    @asynccontextmanager
    async def lifespan(app):
        business_http = httpx.AsyncClient(transport=httpx.MockTransport(services.handle) if mock else None, trust_env=False)
        n8n_http = httpx.AsyncClient(base_url=N8N_URL, timeout=15, trust_env=False,
                                    limits=httpx.Limits(keepalive_expiry=2))
        bot = Bot(settings, clock=clock, http=business_http)
        await bot.start(workers=False)
        app.state.bot, app.state.n8n_http = bot, n8n_http
        app.state.services, app.state.errors = services, errors
        try:
            yield
        finally:
            await bot.stop()
            await business_http.aclose()
            await n8n_http.aclose()

    private = FastAPI(title="LINE n8n Adapter", lifespan=lifespan, docs_url=None, openapi_url=None)
    public = FastAPI(title="LINE Webhook Gateway", docs_url=None, openapi_url=None)

    @private.middleware("http")
    async def authenticate(request, call_next):
        if request.url.path != "/health" and not hmac.compare_digest(request.headers.get(INTERNAL_HEADER, ""), key):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if request.method == "POST":
            # Drain the request before replying, including empty POSTs from Node.
            # A response racing the request terminator can reset Windows sockets.
            body = await request.body()
            if len(body) > 1024 * 1024:
                return JSONResponse({"detail": "Webhook body too large"}, status_code=413)
        return await call_next(request)

    @private.get("/health")
    async def health():
        await private.state.bot.db.one("SELECT 1")
        return {"status": "ok", "mode": "mock" if mock else "live"}

    @private.post("/service/stop")
    async def stop_service():
        if request_stop is None:
            raise HTTPException(409, "Service supervisor unavailable")
        request_stop()
        return {"status": "stopping"}

    @private.get("/service/status")
    async def service_status():
        return json.loads((RUNTIME / "service.json").read_text(encoding="utf-8"))

    @private.post("/ingress")
    async def ingress(request: Request):
        bot = private.state.bot
        body = await raw_body(request)
        if not verify_signature(body, request.headers.get("x-line-signature", ""), settings.line_secret):
            raise HTTPException(400, "Invalid signature")
        try:
            events = json.loads(body)["events"]
            if not isinstance(events, list) or any(not isinstance(e, dict) for e in events):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            raise HTTPException(400, "Invalid webhook payload")
        received = clock.now().timestamp()
        stamp, proof = request.headers.get("x-received-at", ""), request.headers.get("x-received-proof", "")
        if stamp or proof:
            if not hmac.compare_digest(receipt_proof(key, stamp, body), proof):
                raise HTTPException(400, "Invalid receipt")
            try:
                supplied = float(stamp)
                if not 0 < supplied <= received + 1:
                    raise ValueError
            except ValueError:
                raise HTTPException(400, "Invalid receipt")
            received = supplied
        for event in events:
            if services and event.get("replyToken"):
                services.issue(event["replyToken"])
            await bot.receive(event, received)
        return {"status": "ok"}

    @private.post("/jobs/chats")
    async def chats():
        bot = private.state.bot
        async with dispatch_lock:
            await bot.dispatch_chats()
            tasks = list(bot.chat_tasks.values())
        await asyncio.gather(*tasks)
        pending = await bot.db.one("SELECT COUNT(*) AS n FROM jobs WHERE kind='chat' AND status='pending'")
        return {"status": "ok", "pending": pending["n"]}

    @private.post("/jobs/background")
    async def background():
        if background_lock.locked():
            return {"status": "busy"}
        async with background_lock:
            bot = private.state.bot
            await bot.tick()
            await bot.send_due()
            await bot.run_background()
            await bot.send_due()
        return {"status": "ok"}

    @private.post("/errors")
    async def error(request: Request):
        data = await request.json()
        record = {k: str(data.get(k, ""))[:128] for k in ("workflow_id", "execution_id", "node")}
        errors.append(record)
        errors[:] = errors[-100:]
        (RUNTIME / "errors.json").write_text(json.dumps(errors, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"status": "recorded"}

    @public.get("/health")
    async def public_health():
        await private.state.bot.db.one("SELECT 1")
        if not ready():
            return JSONResponse({"status": "starting"}, status_code=503)
        return {"status": "ok"}

    @public.post("/webhook")
    async def webhook(request: Request):
        if not ready():
            return JSONResponse({"error": "service_unavailable"}, status_code=503)
        received_at = str(clock.now().timestamp())
        body = await raw_body(request)
        headers = {"content-type": "application/json", "x-line-signature": request.headers.get("x-line-signature", ""),
                   "x-received-at": received_at, "x-received-proof": receipt_proof(key, received_at, body)}
        try:
            response = await private.state.n8n_http.post(WEBHOOK_PATH, content=body, headers=headers)
        except httpx.HTTPError:
            return JSONResponse({"error": "service_unavailable"}, status_code=503)
        if response.status_code >= 500:
            return JSONResponse({"error": "adapter_unavailable"}, status_code=503)
        return Response(response.content, status_code=response.status_code, media_type="application/json")

    if mock:
        @private.post("/mock/control")
        async def control(value: Control):
            clock.advance(value.advance_seconds)
            for name, field in value.model_dump(exclude_none=True, exclude={"advance_seconds"}).items():
                setattr(services, name, field)
            return {"now": clock.now().isoformat()}

        @private.post("/mock/reset")
        async def reset():
            bot = private.state.bot
            if Path(bot.db.path).resolve() != (RUNTIME / "mock_bot.sqlite3").resolve():
                raise HTTPException(409, "Not a mock database")
            async with background_lock, dispatch_lock:
                await asyncio.gather(*list(bot.chat_tasks.values()))
                async with bot.db.transaction() as conn:
                    for table in ("outbox", "jobs", "memories", "conversation_state", "messages", "weather_snapshots", "users"):
                        await conn.execute(f"DELETE FROM {table}")
                clock.value = MockClock().value
                services.__init__(clock)
                errors.clear()
                bot.user_locks.clear()
            return {"status": "reset", "mode": "mock"}

        @private.get("/mock/evidence")
        async def evidence():
            bot = private.state.bot
            tables = {}
            async with bot.db.transaction() as conn:
                for table in ("users", "messages", "conversation_state", "memories", "weather_snapshots", "jobs", "outbox"):
                    async with conn.execute(f"SELECT * FROM {table}") as cursor:
                        rows = [dict(row) for row in await cursor.fetchall()]
                    for row in rows:
                        if table == "jobs":
                            row.pop("payload", None)
                    tables[table] = rows
            return {"mode": "mock", "now": clock.now().isoformat(), "tables": tables,
                    "replies": services.replies, "pushes": services.pushes, "http_calls": services.calls,
                    "external_network_calls": services.external_network_calls, "errors": errors}

    return private, public
