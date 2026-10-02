"""Everything an export needs, read once from the database."""
from __future__ import annotations

from dataclasses import dataclass, field

from core.models.results import CONFIDENCE_ZH, STATUS_ZH
from core.rules.schema import MEASUREMENT_ZH
from core.version import SOFTWARE_VERSION
from persistence.db import utcnow
from persistence.repo import Repo

STATUS_ORDER = ("FAIL", "WARNING", "UNKNOWN", "PASS")
BASELINE_ZH = {"NEW": "新增", "CHANGED": "有變化", "UNCHANGED": "未變", None: "", "": ""}


class ExportError(Exception):
    """A problem the user can understand; ``message`` is shown as is."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


@dataclass
class Report:
    project: dict
    drawing: dict
    run: dict
    summary: dict
    issues: list[dict]
    evidence: dict[str, dict]
    rules: dict[str, dict]
    extent: tuple[float, float, float, float] | None
    generated_at: str = field(default_factory=utcnow)
    software_version: str = SOFTWARE_VERSION

    @property
    def counts(self) -> dict[str, int]:
        return self.summary.get("counts", {})

    def issues_by_status(self, *statuses: str) -> list[dict]:
        return [i for i in self.issues if i["status"] in statuses]

    def evidence_of(self, issue: dict) -> dict | None:
        return self.evidence.get(issue.get("evidence_id") or "")

    def required_text(self, issue: dict) -> str:
        if issue["required_value"] is None:
            return ""
        unit = "°" if issue["unit"] == "deg" else (f" {issue['unit']}" if issue["unit"] not in ("", "count") else "")
        return f"{issue['required_op']} {issue['required_value']:g}{unit}"

    def measured_text(self, issue: dict) -> str:
        if issue["measured"] is None:
            return ""
        unit = "°" if issue["unit"] == "deg" else (f" {issue['unit']}" if issue["unit"] not in ("", "count") else "")
        return f"{issue['measured']:g}{unit}"

    def evidence_ref_text(self, issue: dict) -> str:
        ev = self.evidence_of(issue)
        if not ev:
            return ""
        page = f" 第{ev['page']}頁" if ev["page"] is not None else ""
        lines = (f"第{ev['line_start']}行" if ev["line_start"] == ev["line_end"]
                 else f"第{ev['line_start']}-{ev['line_end']}行")
        return f"{ev['filename']}{page} {lines}"


def status_zh(status: str) -> str:
    return STATUS_ZH.get(status, status)


def confidence_zh(c: str) -> str:
    return CONFIDENCE_ZH.get(c, c)


def measurement_zh(m: str) -> str:
    return MEASUREMENT_ZH.get(m, m)


def build_report(repo: Repo, run_id: str) -> Report:
    run = repo.get_run(run_id)
    if run is None:
        raise ExportError("找不到這次分析。")
    if run["status"] != "completed":
        raise ExportError("這次分析尚未完成，無法匯出。")
    project = repo.get_project(run["project_id"])
    drawing = repo.get_drawing(run["drawing_id"])
    if project is None or drawing is None:
        raise ExportError("這次分析所屬的專案或圖面已被刪除。")
    issues = repo.all_issues(run_id)
    evidence = {}
    for eid in sorted({i["evidence_id"] for i in issues if i["evidence_id"]}):
        row = repo.get_evidence(project["id"], eid)
        if row:
            evidence[eid] = row
    rules = {r["id"]: r for r in repo.run_ruleset(run_id).get("rules", [])}
    return Report(project=project, drawing=drawing, run=run, summary=run["summary"] or {}, issues=issues,
                  evidence=evidence, rules=rules, extent=repo.drawing_extent(drawing["id"]))
