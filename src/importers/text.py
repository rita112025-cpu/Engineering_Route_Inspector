"""TXT / MD specification import."""
from __future__ import annotations

from pathlib import Path

from core.evidence.chunks import chunk_lines
from core.models.results import EvidenceChunk


class DocumentImportError(Exception):
    def __init__(self, user_message: str, reason: str, detail: str = ""):
        super().__init__(f"{user_message}: {reason}")
        self.user_message = user_message
        self.reason = reason
        self.detail = detail


def decode_bytes(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp950", "big5hkscs", "gb18030"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise DocumentImportError("無法讀取文字檔", "文字編碼無法辨識（請存成 UTF-8）。")


def load_text(path: str | Path, document_id: str, doc_hash: str, filename: str | None = None) -> list[EvidenceChunk]:
    path = Path(path)
    try:
        text = decode_bytes(path.read_bytes())
    except OSError as exc:
        raise DocumentImportError("無法讀取文字檔", "檔案無法開啟。", repr(exc))
    chunks, _ = chunk_lines(text.splitlines(), document_id=document_id, filename=filename or path.name,
                            doc_hash=doc_hash, page=1)
    return chunks
