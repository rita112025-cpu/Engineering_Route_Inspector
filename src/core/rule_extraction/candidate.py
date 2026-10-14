"""Rule candidates: what the specification scan produces *before* anything may run.

A candidate is a rule written down from one sentence of a specification, together with the sentence it came from.
It becomes a real rule only through ``validator`` (every field known, every drawing object named unambiguously,
the sentence really contains the number) and, where that fails, through a person's decision. Nothing here guesses.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

CONFIRMED = "CONFIRMED"
NEEDS_REVIEW = "NEEDS_REVIEW"
CONFLICTING = "CONFLICTING"
NOT_ENFORCEABLE = "NOT_ENFORCEABLE"
STATUSES = (CONFIRMED, NEEDS_REVIEW, CONFLICTING, NOT_ENFORCEABLE)

# Wording of the requirement. Only "mandatory" may become a FAIL rule on its own.
MANDATORY, ADVISORY, UNKNOWN_MODALITY = "mandatory", "advisory", "unknown"

# Reasons a candidate needs a person ("problems"): code -> label shown in the review screen
PROBLEM_LABELS = {
    "SUBJECT_UNMAPPED": "檢查對象對應不到圖面的系統／圖層",
    "TARGET_UNMAPPED": "附近物件對應不到圖面的系統／圖層",
    "ZONE_UNMAPPED": "區域對應不到圖面的圖層",
    "SUBJECT_MISSING": "規範沒有寫出檢查對象",
    "TARGET_MISSING": "規範沒有寫出附近物件（與誰比較）",
    "ZONE_MISSING": "規範沒有寫出區域",
    "AMBIGUOUS_PARTIES": "同一句出現多組對象，無法判斷誰和誰比較",
    "NO_DRAWING": "尚未匯入圖面，無法對應圖層",
    "NO_UNIT": "數值沒有單位（不會自行當成 mm）",
    "NO_VALUE": "找不到數值",
    "NO_DIRECTION": "無法判斷是「不得小於」還是「不得大於」",
    "BOTH_DIRECTIONS": "同一句同時出現兩種比較方向",
    "UNSPECIFIED_AXIS": "沒有說明是水平淨距還是垂直淨距",
    "UNSUPPORTED_DIRECTION": "目前的規則只支援另一個比較方向",
    "ADVISORY": "用語是「宜／建議／儘量」，不是強制規定",
    "NO_MODAL": "沒有「應／不得」等規定性用語",
    "INHERITED_PARTIES": "對象是沿用前一個子句，需確認",
    "MISSING_TOLERANCE": "沒有寫出容許偏差（度）",
    "EVIDENCE_MISSING": "找不到這條規則的規範原文（沒有依據不會自動採用）",
    "EVIDENCE_MISMATCH": "原文與擷取的數值對不上",
    "UNIT_MISMATCH": "單位與這類規則不符",
}


@dataclass
class RawCandidate:
    """The parser's output for one sentence. Independent of any drawing, glossary or review decision, so it can be
    stored per document and reused until the document changes."""
    semantic_key: str
    family_key_text: str
    template_id: str | None
    measurement: str | None
    operator: str | None
    value: float | None
    unit: str | None
    value_text: str
    subject_text: str
    target_text: str
    zone_text: str
    modality: str
    problems: list[str]
    enforceable: bool
    sentence: str
    reason: str
    # where it came from
    document_id: str
    document: str
    evidence_id: str
    page: int | None
    line_start: int
    line_end: int
    section: str

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "RawCandidate":
        return RawCandidate(**{k: d[k] for k in RawCandidate.__dataclass_fields__})


def norm_text(text: str) -> str:
    """Comparison form of a phrase: NFKC, case-folded, no spaces or punctuation."""
    t = unicodedata.normalize("NFKC", text or "").casefold()
    return re.sub(r"[\s,.;:、，。；：()\"'「」『』《》\-_/]+", "", t)


def fmt_num(value: float | None) -> str:
    return "" if value is None else f"{value:.6f}".rstrip("0").rstrip(".")


def build_semantic_key(measurement: str | None, operator: str | None, value: float | None, unit: str | None,
                       subject: str, target: str, zone: str, sentence: str, complete: bool) -> str:
    """Identity of a rule's *meaning*: type, parties, comparison, normalized value and unit.

    Two sentences that say the same thing get the same key; a paragraph moving by a line does not change it.
    A candidate the parser could not complete additionally carries a hash of its sentence, so two different
    unfinished sentences are never merged."""
    parts = [measurement or "?", norm_text(subject), norm_text(target) or norm_text(zone), operator or "?",
             fmt_num(value), unit or "?"]
    key = "|".join(parts)
    if not complete:
        key += "|s:" + hashlib.sha256(norm_text(sentence).encode("utf-8")).hexdigest()[:10]
    return key


def stable_candidate_id(document_sha256: str, semantic_key: str) -> str:
    """Same document content + same meaning = same ID after every rescan (review decisions stay attached)."""
    digest = hashlib.sha256(f"{document_sha256}|{semantic_key}".encode("utf-8")).hexdigest()[:16]
    return f"cand_{digest}"


@dataclass
class CandidateView:
    """A candidate as the API shows it: parser output plus the current status, selectors and decision state."""
    id: str
    status: str
    bucket: str                              # usable / review / conflict / not_enforceable / off
    raw: RawCandidate
    template_id: str | None = None
    measurement: str | None = None
    operator: str | None = None
    value: float | None = None
    unit: str | None = None
    requirement: str = ""
    problems: list[str] = field(default_factory=list)
    fix: dict = field(default_factory=dict)
    subject_selector: dict | None = None
    target_selector: dict | None = None
    zone_selector: dict | None = None
    severity: str = "FAIL"
    missing: list[dict] = field(default_factory=list)       # [{"code", "label"}]
    reason: str = ""
    warnings: list[str] = field(default_factory=list)
    decision: dict = field(default_factory=dict)
    enabled: bool = False
    confirmed_by: str | None = None          # "auto" | "user"
    conflict_group: str | None = None
    conflict_with: list[str] = field(default_factory=list)
    conflict_options: list[dict] = field(default_factory=list)
    duplicates: list[dict] = field(default_factory=list)
