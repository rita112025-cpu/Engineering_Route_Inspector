"""API errors: every failure reaches the user as a Chinese sentence plus a stable code."""
from __future__ import annotations

import logging
import re
import traceback

from starlette.requests import Request
from starlette.responses import JSONResponse

from core.rules.schema import RuleError
from exporters.report import ExportError
from importers.dxf import DrawingImportError
from importers.text import DocumentImportError
from persistence.db import MigrationError
from persistence.storage import StorageError, UploadTooLarge

log = logging.getLogger("eri")
_CTRL = re.compile(r"[\x00-\x1f\x7f\u2028\u2029]")


def clean_log(text) -> str:
    """Text safe to write as one log line: control characters (newlines!) are shown escaped."""
    return _CTRL.sub(lambda m: f"\\x{ord(m.group()):02x}", str(text))


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, **extra):
        super().__init__(message)
        self.status, self.code, self.message, self.extra = status, code, message, extra


def not_found(what: str) -> ApiError:
    return ApiError(404, "NOT_FOUND", f"找不到{what}。")


def bad_request(message: str, code: str = "BAD_REQUEST", **extra) -> ApiError:
    return ApiError(400, code, message, **extra)


def payload(status: int, code: str, message: str, **extra) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message, **extra}}, status_code=status)


async def api_error_handler(request: Request, exc: ApiError):
    return payload(exc.status, exc.code, exc.message, **exc.extra)


async def known_error_handler(request: Request, exc: Exception):
    if isinstance(exc, UploadTooLarge):
        return payload(413, "TOO_LARGE", str(exc))
    if isinstance(exc, RuleError):
        return payload(400, "RULES_INVALID", f"規則有 {len(exc.errors)} 個問題，請修正後再儲存。", errors=exc.errors)
    if isinstance(exc, DrawingImportError):
        log.info("drawing import failed: %s", clean_log(exc.detail))        # detail may hold file paths: log only
        return payload(400, "DRAWING_UNREADABLE", f"{exc.user_message}：{exc.reason}")
    if isinstance(exc, DocumentImportError):
        log.info("document import failed: %s", clean_log(exc.detail))
        return payload(400, "DOCUMENT_UNREADABLE", f"{exc.user_message}：{exc.reason}")
    if isinstance(exc, ExportError):
        return payload(409, "EXPORT_FAILED", exc.message)
    if isinstance(exc, StorageError):
        return payload(400, "BAD_FILE", str(exc))
    if isinstance(exc, MigrationError):
        return payload(500, "DATABASE_VERSION", str(exc))
    return await unexpected_error_handler(request, exc)


async def unexpected_error_handler(request: Request, exc: Exception):
    """Never leak a traceback to the browser; keep it in the server log for diagnostics."""
    log.error("unhandled %s on %s %s\n%s", type(exc).__name__, request.method, clean_log(request.url.path),
              "".join(traceback.format_exception(exc))[-3000:])
    return payload(500, "INTERNAL_ERROR", "程式發生未預期的錯誤。細節已寫入記錄檔，請在診斷頁面匯出診斷資料。")


KNOWN = (UploadTooLarge, RuleError, DrawingImportError, DocumentImportError, ExportError, StorageError,
         MigrationError)
