"""單一 Web worker 的 FastAPI 入口。"""
from contextlib import asynccontextmanager
import json
import logging
import os
import socket

from fastapi import FastAPI, HTTPException, Request

from weather_bot.config import Settings
from weather_bot.line_io import verify_signature
from weather_bot.tunnel import LocalTunnel
from weather_bot.workers import Bot


def create_app(settings=None, *, bot=None, local_tunnel=False):
    @asynccontextmanager
    async def lifespan(app):
        runtime = bot or Bot(settings or Settings.from_env())
        if not runtime.settings.line_secret:
            raise ValueError("LINE_CHANNEL_SECRET 未設定")
        tunnel = LocalTunnel(runtime.settings) if local_tunnel else None
        try:
            await runtime.start()
            app.state.bot = runtime
            if tunnel:
                await tunnel.start()
            yield
        finally:
            if tunnel:
                await tunnel.stop()
            await runtime.stop()

    app = FastAPI(title="LINE Weather Galgame Bot", lifespan=lifespan)

    @app.get("/health")
    async def health():
        await app.state.bot.db.one("SELECT 1")
        return {"status": "ok"}

    @app.post("/webhook")
    async def webhook(request: Request):
        runtime = app.state.bot
        received_at = runtime.clock.now().timestamp()
        # 收取原始 bytes 後驗簽；不重新序列化內容。
        body = await request.body()
        if len(body) > 1024 * 1024:
            raise HTTPException(413, "Webhook body too large")
        if not verify_signature(body, request.headers.get("x-line-signature", ""), runtime.settings.line_secret):
            raise HTTPException(400, "Invalid signature")
        try:
            payload = json.loads(body)
            events = payload["events"]
            if not isinstance(events, list) or any(not isinstance(e, dict) for e in events):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            raise HTTPException(400, "Invalid webhook payload")
        for event in events:
            await runtime.receive(event, received_at)
        return {"status": "ok"}

    return app


app = create_app()


def _bind_listener(port=18567):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name == "nt":
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", port))
        listener.listen(128)
        return listener
    except BaseException:
        listener.close()
        raise


def main():
    import uvicorn
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx 的 INFO 日誌可能包含 CWA URL query 的授權碼。
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = Settings.from_env()
    # 先保留連接埠；若已有服務佔用，不啟動 Bot 或更動 LINE 設定。
    listener = _bind_listener()
    try:
        config = uvicorn.Config(create_app(settings, local_tunnel=settings.local_tunnel_enabled),
                                host="127.0.0.1", port=18567, workers=1)
        uvicorn.Server(config).run(sockets=[listener])
    except KeyboardInterrupt:
        pass
    finally:
        listener.close()


if __name__ == "__main__":
    main()
