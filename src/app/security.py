"""Request guards, written as plain ASGI middleware so they run before any routing.

* HostGuard      - Host header must be one of our loopback names (DNS-rebinding defence).
* Origin check   - a request that carries an Origin must come from our own page.
* CSRF token     - state-changing API calls need the per-process token the page was served with.
* BodyLimit      - request bodies are counted while they stream in and cut off at the limit.
* security headers on every response.
"""
from __future__ import annotations

import hmac
import json
import re

from starlette.datastructures import Headers

from .config import AppConfig

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
TOKEN_HEADER = "x-eri-token"
UPLOAD_PATH = re.compile(r"^/api/projects/[^/]+/(drawings|documents)$")

CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
SECURITY_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"cross-origin-resource-policy", b"same-origin"),
    (b"x-frame-options", b"DENY"),
]


def error_body(code: str, message: str, **extra) -> bytes:
    return json.dumps({"error": {"code": code, "message": message, **extra}}, ensure_ascii=False).encode("utf-8")


async def send_error(send, status: int, code: str, message: str) -> None:
    body = error_body(code, message)
    await send({"type": "http.response.start", "status": status, "headers": [
        (b"content-type", b"application/json; charset=utf-8"), (b"content-length", str(len(body)).encode()),
        (b"cache-control", b"no-store"), *SECURITY_HEADERS]})
    await send({"type": "http.response.body", "body": body})


class SecurityMiddleware:
    def __init__(self, app, config: AppConfig, token: str):
        self.app = app
        self.config = config
        self.token = token

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            return await self.app(scope, receive, send)
        if scope["type"] != "http":                  # no websockets are served
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            return
        headers = Headers(scope=scope)
        if headers.get("host", "").lower() not in self.config.allowed_hosts:
            return await send_error(send, 403, "BAD_HOST", "不允許的主機名稱。此服務只接受本機（127.0.0.1）的連線。")
        origin = headers.get("origin")
        if origin is not None and origin.lower() not in self.config.allowed_origins:
            return await send_error(send, 403, "BAD_ORIGIN", "不允許的來源網頁。")
        path = scope["path"]
        if path.startswith("/api/") and scope["method"] not in SAFE_METHODS:
            site = headers.get("sec-fetch-site")
            if site is not None and site not in ("same-origin", "none"):
                return await send_error(send, 403, "BAD_ORIGIN", "不允許跨網站的請求。")
            if not hmac.compare_digest(headers.get(TOKEN_HEADER, "").encode(), self.token.encode()):
                return await send_error(send, 403, "BAD_TOKEN", "缺少或錯誤的安全權杖，請重新整理頁面。")

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                hs = list(message.get("headers", []))
                present = {k.lower() for k, _ in hs}
                for k, v in SECURITY_HEADERS:
                    if k not in present:
                        hs.append((k, v))
                if path.startswith("/api/") and b"cache-control" not in present:
                    hs.append((b"cache-control", b"no-store"))
                if not path.startswith("/api/") and b"content-security-policy" not in present:
                    hs.append((b"content-security-policy", CSP.encode()))
                message = dict(message, headers=hs)
            await send(message)

        await self.app(scope, receive, send_with_headers)


class BodyTooLarge(Exception):
    pass


class BodyLimitMiddleware:
    """Reject oversized bodies up front (Content-Length) and while streaming (chunked or lying clients)."""

    def __init__(self, app, config: AppConfig):
        self.app = app
        self.config = config

    def limit_for(self, scope) -> int:
        m = UPLOAD_PATH.match(scope["path"])
        if m and scope["method"] == "POST":
            mb = self.config.max_drawing_mb if m.group(1) == "drawings" else self.config.max_document_mb
            return mb * 1024 * 1024 + 64 * 1024        # multipart framing
        return self.config.max_json_kb * 1024

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        limit = self.limit_for(scope)
        declared = Headers(scope=scope).get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > limit:
            return await send_error(send, 413, "TOO_LARGE", f"檔案或資料太大（上限 {limit // (1024 * 1024)} MB）。")
        seen = 0
        started = False

        async def counting_receive():
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > limit:
                    raise BodyTooLarge
            return message

        async def tracking_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, counting_receive, tracking_send)
        except BodyTooLarge:
            if not started:
                await send_error(send, 413, "TOO_LARGE", f"檔案或資料太大（上限 {limit // (1024 * 1024)} MB）。")
