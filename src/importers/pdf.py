"""PDF specification import (PyMuPDF). Text-layer only; no OCR."""
from __future__ import annotations

from pathlib import Path

from core.evidence.chunks import chunk_lines
from core.models.results import EvidenceChunk
from .text import DocumentImportError


def load_pdf(path: str | Path, document_id: str, doc_hash: str, filename: str | None = None) -> tuple[list[EvidenceChunk], list[str]]:
    try:
        import fitz  # PyMuPDF
    except ImportError as exc:
        raise DocumentImportError("無法讀取 PDF", "缺少 PyMuPDF 套件，請重新執行 start-ui.bat 安裝。", repr(exc))
    path = Path(path)
    warnings: list[str] = []
    try:
        # Open from memory, not from the path: when fitz.open() fails, PyMuPDF's half-built Document (kept alive by the
        # exception's traceback) holds the file open, and on Windows the caller could then not delete the rejected
        # upload (WinError 32). Uploads are limited to max_document_mb, so reading the bytes is cheap.
        doc = fitz.open(stream=path.read_bytes(), filetype="pdf")
    except Exception as exc:  # noqa: BLE001
        raise DocumentImportError("無法讀取 PDF", "檔案損毀或不是 PDF。", repr(exc))
    if doc.needs_pass:
        raise DocumentImportError("無法讀取 PDF", "PDF 有密碼保護。")
    chunks: list[EvidenceChunk] = []
    section = ""
    empty_pages = 0
    with doc:
        for pno, page in enumerate(doc, start=1):
            text = page.get_text("text")
            if not text.strip():
                empty_pages += 1
                continue
            page_chunks, section = chunk_lines(text.splitlines(), document_id=document_id,
                                               filename=filename or path.name, doc_hash=doc_hash,
                                               page=pno, section=section)
            chunks.extend(page_chunks)
    if empty_pages:
        warnings.append(f"{empty_pages} 頁沒有文字層（可能是掃描影像），這些頁無法作為證據；本工具不做 OCR。")
    return chunks, warnings
