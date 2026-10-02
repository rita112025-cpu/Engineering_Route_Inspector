"""Rule evaluation results and evidence records."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

STATUSES = ("FAIL", "WARNING", "PASS", "UNKNOWN")
CONFIDENCES = ("CONFIRMED", "INFERRED", "UNKNOWN")
CONFIDENCE_ZH = {"CONFIRMED": "已確認", "INFERRED": "推算", "UNKNOWN": "無法判定"}
STATUS_ZH = {"FAIL": "不合格", "WARNING": "警告", "PASS": "合格", "UNKNOWN": "無法判定"}


@dataclass
class EvidenceChunk:
    id: str
    document_id: str
    filename: str
    page: int | None
    line_start: int
    line_end: int
    section: str
    text: str
    hash: str

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class RuleResult:
    rule_id: str
    rule_name: str
    status: str                     # FAIL / WARNING / PASS / UNKNOWN
    severity: str                   # severity declared on the rule
    measurement: str
    subject_ids: list[str]
    subject_handles: list[str]
    target_ids: list[str] = field(default_factory=list)
    target_handles: list[str] = field(default_factory=list)
    layer: str = ""
    system: str = ""
    location: tuple[float, float] | None = None
    measured: float | None = None   # in rule units
    required_op: str = ""
    required_value: float | None = None
    unit: str = ""
    message: str = ""
    reason: str = ""
    fix: str = ""
    confidence: str = "INFERRED"
    evidence_id: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    issue_id: str = ""

    @property
    def handles(self) -> list[str]:
        return sorted(set(self.subject_handles) | set(self.target_handles))

    def to_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["location"] = list(self.location) if self.location else None
        return d
