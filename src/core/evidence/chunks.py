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


_NUM_RE = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?")


def numbers_in(text: str) -> list[float]:
    """Numbers written in ``text`` (full-width digits and thousands separators handled)."""
    t = unicodedata.normalize("NFKC", text or "")
    return [float(m.replace(",", "")) for m in _NUM_RE.findall(t)]


_UNIT_WORDS = {
    "mm": "mm", "毫米": "mm", "公釐": "mm", "millimeter": "mm", "millimeters": "mm", "millimetre": "mm", "millimetres": "mm",
    "cm": "cm", "公分": "cm", "centimeter": "cm", "centimeters": "cm", "centimetre": "cm", "centimetres": "cm",
    "m": "m", "公尺": "m", "米": "m", "meter": "m", "meters": "m", "metre": "m", "metres": "m",
    "ft": "ft", "foot": "ft", "feet": "ft", "呎": "ft", "英尺": "ft",
    "in": "in", "inch": "in", "inches": "in", "吋": "in", "英吋": "in",
    "°": "deg", "度": "deg", "deg": "deg", "degree": "deg", "degrees": "deg",
}
_QTY_RE = re.compile(
    r"(?<![\d.])(?<!\d,)(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*("
    + "|".join(re.escape(w) for w in sorted(_UNIT_WORDS, key=len, reverse=True))
    + r")(?![A-Za-z0-9²³/^·×])", re.IGNORECASE)


def quantities_in(text: str) -> list[tuple[float, str, str]]:
    """(value, canonical unit, matched text) for every number written *with* a unit.

    Numbers without a unit (clause numbers, counts) are deliberately not quantities: they cannot
    support a length or an angle.
    """
    t = unicodedata.normalize("NFKC", text or "")
    out = []
    for m in _QTY_RE.finditer(t):
        unit = _UNIT_WORDS[m.group(2).lower()]
        if unit == "in" and re.match(r"\s+[A-Za-z]", t[m.end():]):     # "300 in the drawing", not inches
            continue
        out.append((float(m.group(1).replace(",", "")), unit, m.group(0)))
    return out


def _inside_larger_number(nt: str, start: int, end: int) -> bool:
    """True when nt[start:end] begins or ends in the middle of a number (``300`` inside ``1300``/``300.5``)."""
    if nt[start].isdigit():
        if start > 0 and nt[start - 1].isdigit():
            return True
        if start > 1 and nt[start - 1] in ".," and nt[start - 2].isdigit():
            return True
    if nt[end - 1].isdigit():
        if end < len(nt) and nt[end].isdigit():
            return True
        if end + 1 < len(nt) and nt[end] in ".," and nt[end + 1].isdigit():
            return True
    return False


def find_by_quote(chunks: Iterable[EvidenceChunk], quote: str, document: str | None = None) -> EvidenceChunk | None:
    """Exact (whitespace/width-insensitive) substring lookup. No fuzzy guessing.

    A quote that starts or ends with a digit must start/end on a number
    boundary, so ``300 mm`` is never found inside ``1300 mm`` or ``0.300 mm``.
    """
    q = normalize(quote or "")
    if not q:
        return None
    for c in chunks:
        if document and c.filename.casefold() != document.casefold():
            continue
        nt = normalize(c.text)
        pos = nt.find(q)
        while pos != -1:
            if not _inside_larger_number(nt, pos, pos + len(q)):
                return c
            pos = nt.find(q, pos + 1)
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
