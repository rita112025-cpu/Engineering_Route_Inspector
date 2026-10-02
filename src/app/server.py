"""ASGI application factory and the command line entry point."""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import secrets
import socket
import sys
import time
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from jobs.manager import JobManager
from persistence.db import Database
from persistence.storage import Storage
from .api import ROUTES
from .config import DEFAULT_PORT, AppConfig, ConfigError, default_data_dir
from .errors import KNOWN, clean_log, ApiError, api_error_handler, known_error_handler, unexpected_error_handler
from .security import BodyLimitMiddleware, SecurityMiddleware
from .state import AppState

TOKEN_PLACEHOLDER = "__ERI_TOKEN__"
log = logging.getLogger("eri")


def setup_logging(data_dir: Path) -> None:
    logs = data_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(logs / "server.log", maxBytes=1_000_000, backupCount=3,
                                                   encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.handlers = [handler]
    log.setLevel(logging.INFO)
    log.propagate = False


class AccessLogMiddleware:
    """One line per request: method, path, status, milliseconds. Bodies and query strings are never logged."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        t0 = time.perf_counter()
        status = 0

        async def send2(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)
        try:
            await self.app(scope, receive, send2)
        finally:
            log.info("%s %s -> %s %.0f ms", clean_log(scope["method"]), clean_log(scope["path"]), status, (time.perf_counter() - t0) * 1000)


def create_app(config: AppConfig) -> Starlette:
    token = secrets.token_urlsafe(32)

    @asynccontextmanager
    async def lifespan(app: Starlette):
        data_dir = config.data_dir
        data_dir.mkdir(parents=True, exist_ok=True)
        setup_logging(data_dir)
        storage = Storage(data_dir)
        db = Database(storage.db_path)          # migrates the schema
        storage.cleanup_partial_uploads()
        manager = JobManager(data_dir, max_workers=config.max_workers)
        recovery = manager.start()
        app.state.eri = AppState(config=config, storage=storage, db=db, manager=manager, token=token,
                                 recovery=recovery)
        log.info("started on %s recovery=%s", config.url, recovery)
        try:
            yield
        finally:
            manager.stop()
            db.close()
            log.info("stopped")

    def index(request: Request):
        page = config.static_dir / "index.html"
        if not page.is_file():
            return JSONResponse({"error": {"code": "NO_UI", "message": "找不到介面檔案（static/index.html）。"}},
                                status_code=404)
        html = page.read_text(encoding="utf-8").replace(TOKEN_PLACEHOLDER, token)
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    def not_found(request: Request, exc):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": {"code": "NOT_FOUND", "message": "找不到這個網址。"}}, status_code=404)
        return HTMLResponse("找不到頁面", status_code=404)

    routes = [Route("/", index), *ROUTES,
              Mount("/static", app=StaticFiles(directory=str(config.static_dir), check_dir=False), name="static")]
    handlers = {ApiError: api_error_handler, 404: not_found, Exception: unexpected_error_handler}
    handlers.update({exc: known_error_handler for exc in KNOWN})
    return Starlette(
        routes=routes, exception_handlers=handlers,
        middleware=[Middleware(AccessLogMiddleware), Middleware(SecurityMiddleware, config=config, token=token),
                    Middleware(BodyLimitMiddleware, config=config), Middleware(GZipMiddleware, minimum_size=2048)],
        lifespan=lifespan)


def find_free_port(host: str, start: int, tries: int = 20) -> int:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    for port in range(start, start + tries):
        with socket.socket(family, socket.SOCK_STREAM) as s:
            try:
                s.bind((host, port))
                return port
            except OSError:
                continue
    raise OSError(f"連接埠 {start}–{start + tries - 1} 都被占用，請關閉其他程式或用 --port 指定其他連接埠。")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app", description="Engineering Route Inspector (local web UI)")
    ap.add_argument("--host", default="127.0.0.1", help="只接受本機位址（預設 127.0.0.1）")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--data-dir", default=None, help="資料夾（預設：專案目錄下的 data）")
    ap.add_argument("--no-browser", action="store_true", help="不要自動開啟瀏覽器")
    ap.add_argument("--diagnose", action="store_true", help="只做健康檢查並印出結果，不啟動服務")
    ap.add_argument("--json", action="store_true", help="搭配 --diagnose：以 JSON 輸出")
    args = ap.parse_args(argv)
    if args.diagnose:
        from . import diagnostics
        info = diagnostics.collect(Path(args.data_dir) if args.data_dir else default_data_dir(), deep=True)
        info.pop("log_tail", None)
        if args.json:
            print(json.dumps(info, ensure_ascii=False, indent=2))
        else:
            print(f"程式版本 {info['software_version']}　Python {info['python']}")
            for c in info["checks"]:
                print(("[正常] " if c["ok"] else "[問題] ") + c["name"] + (f"：{c['detail']}" if c["detail"] else ""))
            print("結果：" + ("一切正常" if info["healthy"] else "有項目需要注意（見上方 [問題]）"))
        return 0 if info["healthy"] else 1
    try:
        host_probe = AppConfig(data_dir=Path("."), host=args.host, port=args.port)   # validates the host
        port = find_free_port(host_probe.host, args.port)
        config = AppConfig(data_dir=Path(args.data_dir) if args.data_dir else default_data_dir(),
                           host=args.host, port=port)
    except (ConfigError, OSError) as exc:
        print(f"無法啟動：{exc}", file=sys.stderr)
        return 2
    import uvicorn
    app = create_app(config)
    print(f"Engineering Route Inspector：{config.url}", flush=True)
    print(f"資料位置：{config.data_dir.resolve()}", flush=True)
    print("關閉這個視窗即可結束程式。", flush=True)
    if not args.no_browser:
        import threading
        threading.Timer(1.2, lambda: webbrowser.open(config.url)).start()
    uvicorn.run(app, host=config.host, port=config.port, log_level="warning", access_log=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
