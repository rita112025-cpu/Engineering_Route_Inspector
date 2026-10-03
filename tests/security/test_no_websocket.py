"""No websocket is served, and no websocket library is even imported.

SecurityMiddleware has always refused websocket scopes. With uvicorn's default ``ws="auto"`` the server would
still import whichever of ``websockets`` / ``wsproto`` is installed, and with some version combinations that import
raises a DeprecationWarning (``websockets.legacy``), which fails the server start under ``pytest -W error``.
"""
from __future__ import annotations

import socket
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from app.server import main

SRC = Path(__file__).resolve().parents[2] / "src"


def test_production_server_is_started_without_a_websocket_implementation(tmp_path, monkeypatch):
    import uvicorn
    from tests.conftest import free_port
    captured = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: captured.update(kw))
    code = main(["--data-dir", str(tmp_path / "d"), "--no-browser", "--port", str(free_port())])
    assert code == 0
    assert captured["ws"] == "none" and captured["host"] == "127.0.0.1"


def _poisoned_import_check(tmp_path, ws: str) -> subprocess.CompletedProcess:
    """Run a uvicorn config load in a clean interpreter where importing a websocket library is an error."""
    for name in ("websockets", "wsproto"):
        pkg = tmp_path / "poison" / name
        pkg.mkdir(parents=True, exist_ok=True)
        (pkg / "__init__.py").write_text(f"raise RuntimeError('{name} must not be imported')\n", encoding="utf-8")
    script = textwrap.dedent(f"""
        import sys
        from app.config import AppConfig
        from app.server import create_app
        import uvicorn
        app = create_app(AppConfig(data_dir=r"{tmp_path / 'data'}", port=1))
        cfg = uvicorn.Config(app, host="127.0.0.1", port=1, ws="{ws}", lifespan="off")
        cfg.load()
        loaded = sorted(m for m in sys.modules if m.split(".")[0] in ("websockets", "wsproto")
                        or m.startswith("uvicorn.protocols.websockets"))
        print("LOADED", loaded)
    """)
    env = {"PYTHONPATH": str(tmp_path / "poison") + ";" + str(SRC) if sys.platform == "win32"
           else str(tmp_path / "poison") + ":" + str(SRC), "PATH": ""}
    for k in ("SYSTEMROOT", "SystemRoot", "WINDIR", "TEMP", "TMP"):
        import os
        if k in os.environ:
            env[k] = os.environ[k]
    return subprocess.run([sys.executable, "-W", "error", "-c", script], capture_output=True, text=True, env=env, timeout=60)


def test_ws_none_loads_no_websocket_library(tmp_path):
    ok = _poisoned_import_check(tmp_path, "none")
    assert ok.returncode == 0, ok.stderr
    assert "LOADED []" in ok.stdout


def test_the_poisoned_library_check_is_meaningful(tmp_path):
    """Control: with ws="auto" the same interpreter does try to import a websocket library (and so fails here)."""
    bad = _poisoned_import_check(tmp_path, "auto")
    assert bad.returncode != 0 and "must not be imported" in bad.stderr, (bad.stdout, bad.stderr)


def _raw(port: int, request: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
        s.sendall(request)
        s.shutdown(socket.SHUT_WR)
        data = b""
        while chunk := s.recv(65536):
            data += chunk
            if len(data) > 1_000_000:
                break
    return data


@pytest.mark.parametrize("path", ["/", "/api/health", "/ws", "/api/projects"])
def test_websocket_upgrade_is_never_accepted(live, path):
    req = (f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{live.port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
           "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n").encode()
    status = _raw(live.port, req).split(b"\r\n", 1)[0]
    assert status.startswith(b"HTTP/1.1 ") and b" 101 " not in status and b"Switching" not in status, status


def test_websocket_upgrade_with_a_foreign_origin_or_host_is_refused(live):
    base = ("GET / HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n{extra}\r\n")
    evil_origin = base.format(host=f"127.0.0.1:{live.port}", extra="Origin: http://evil.example\r\n").encode()
    evil_host = base.format(host="evil.example", extra="").encode()
    assert _raw(live.port, evil_origin).startswith(b"HTTP/1.1 403")
    assert _raw(live.port, evil_host).startswith(b"HTTP/1.1 403")


def test_health_and_page_still_work_without_websockets(live):
    assert live.raw.get("/api/health").json()["status"] == "ok"
    assert live.raw.get("/").status_code == 200
