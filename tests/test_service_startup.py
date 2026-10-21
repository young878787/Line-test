import socket

import pytest

from weather_bot import main


def test_reusable_existing_listener_cannot_be_shared():
    # Uvicorn 在 Windows 會用 SO_REUSEADDR；第二個服務仍須拒絕綁定。
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as existing:
        existing.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        existing.bind(("127.0.0.1", 0))
        existing.listen(1)
        with pytest.raises(OSError):
            main._bind_listener(existing.getsockname()[1])


def test_occupied_port_stops_before_app_and_tunnel_start(monkeypatch):
    with main._bind_listener(0) as existing:
        port = existing.getsockname()[1]
        bind = main._bind_listener
        monkeypatch.setattr(main, "_bind_listener", lambda: bind(port))
        monkeypatch.setattr(main.Settings, "from_env", lambda: main.Settings())

        def unexpected(*args, **kwargs):
            pytest.fail("連接埠已佔用時不應建立應用程式、Bot 或隧道")

        monkeypatch.setattr(main, "create_app", unexpected)
        with pytest.raises(OSError):
            main.main()
