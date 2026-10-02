"""Split document text into traceable evidence chunks and look them up.

Chunks never contain generated text: every chunk is a verbatim slice of a
source document with its page and line range.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Iterable

from core.models.results import EvidenceChunk

HEADING_RE = re.compile(
    r"^\s*(#{1,6}\s+.+|\d+(\.\d+)*[\s、.．)]\s*\S.*|第[一二三四五六七八九十百零〇\d]+[章節條款].*|[A-Z]\.\d+(\.\d+)*\s+\S.*)$")
MAX_CHUNK_LINES = 12


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def evidence_id(doc_hash: str, page: int | None, line_start: int, text: str) -> str:
    key = f"{doc_hash}|{page if page is not None else '-'}|{line_start}|{sha256_text(text)}"
    return "EV-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:12].upper()


def is_heading(line: str) -> bool:
    s = line.strip()
    return bool(s) and len(s) <= 80 and bool(HEADING_RE.match(s))


def chunk_lines(lines: list[str], *, document_id: str, filename: str, doc_hash: str,
                page: int | None, section: str = "", line_offset: int = 0) -> tuple[list[EvidenceChunk], str]:
    """Group consecutive non-blank lines into paragraphs (max MAX_CHUNK_LINES).

    Line numbers are 1-based within the page (or document for TXT/DOCX).
    Returns (chunks, last_section) so sections can continue across pages.
    """
    chunks: list[EvidenceChunk] = []
    buf: list[tuple[int, str]] = []

    def flush():
        nonlocal buf
        if not buf:
            return
        text = "\n".join(t for _, t in buf).strip()
        if text:
            ls, le = buf[0][0], buf[-1][0]
            chunks.append(EvidenceChunk(
                id=evidence_id(doc_hash, page, ls, text), document_id=document_id, filename=filename,
                page=page, line_start=ls, line_end=le, section=section, text=text, hash=sha256_text(text)))
        buf = []

    for i, raw in enumerate(lines, start=1 + line_offset):
        line = raw.rstrip()
        if not line.strip():
            flush()
            continue
        if is_heading(line):
            flush()
            section = line.strip().lstrip("#").strip()
            buf.append((i, line))
            flush()
            continue
        buf.append((i, line))
        if len(buf) >= MAX_CHUNK_LINES:
            flush()
    flush()
    return chunks, section


def normalize(text: str) -> str:
    t = unicodedata.normalize("NFKC", text).casefold()
    return re.sub(r"\s+", "", t)


def find_by_quote(chunks: Iterable[EvidenceChunk], quote: str, document: str | None = None) -> EvidenceChunk | None:
    """Exact (whitespace/width-insensitive) substring lookup. No fuzzy guessing."""
    q = normalize(quote or "")
    if not q:
        return None
    for c in chunks:
        if document and c.filename.casefold() != document.casefold():
            continue
        if q in normalize(c.text):
            return c
    return None


def search(chunks: Iterable[EvidenceChunk], query: str, limit: int = 20) -> list[EvidenceChunk]:
    terms = [normalize(t) for t in (query or "").split() if t.strip()]
    if not terms:
        return []
    out = []
    for c in chunks:
        n = normalize(c.text + c.section)
        if all(t in n for t in terms):
            out.append(c)
            if len(out) >= limit:
                break
    return out
