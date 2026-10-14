"""Storage for the specification scan: the chosen source folder, scanned files, rule candidates and review decisions.

Review decisions live in their own table, keyed by the candidate's stable ID, so re-extracting a changed document
never throws away what a person already decided about a requirement that did not change.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Iterable

from core.models.results import EvidenceChunk
from core.rule_extraction.candidate import RawCandidate
from .db import transaction, utcnow


def _j(v) -> str:
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"))


class SpecStore:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def _q(self, sql: str, args=()) -> list[dict]:
        return [dict(r) for r in self.conn.execute(sql, args)]

    def _one(self, sql: str, args=()) -> dict | None:
        r = self.conn.execute(sql, args).fetchone()
        return dict(r) if r is not None else None

    # -- settings --------------------------------------------------------------------------------------------------
    def settings(self, project_id: str) -> dict:
        row = self._one("SELECT * FROM spec_settings WHERE project_id = ?", (project_id,))
        if row is None:
            return {"source_path": None, "glossary": {}, "last_scan": None}
        return {"source_path": row["source_path"], "glossary": json.loads(row["glossary_json"] or "{}"),
                "last_scan": json.loads(row["last_scan_json"]) if row["last_scan_json"] else None}

    def _upsert(self, project_id: str, column: str, value) -> None:
        self.conn.execute(
            f"INSERT INTO spec_settings(project_id, {column}, updated_at) VALUES (?,?,?) "
            f"ON CONFLICT(project_id) DO UPDATE SET {column} = excluded.{column}, updated_at = excluded.updated_at",
            (project_id, value, utcnow()))

    def set_source_path(self, project_id: str, path: str | None) -> None:
        with transaction(self.conn):
            self._upsert(project_id, "source_path", path)

    def set_glossary(self, project_id: str, glossary: dict) -> None:
        with transaction(self.conn):
            self._upsert(project_id, "glossary_json", _j(glossary))

    def set_last_scan(self, project_id: str, summary: dict) -> None:
        with transaction(self.conn):
            self._upsert(project_id, "last_scan_json", _j(summary))

    # -- scanned files ---------------------------------------------------------------------------------------------
    def files(self, project_id: str, root_path: str) -> dict[str, dict]:
        return {r["rel_path"]: r for r in self._q(
            "SELECT * FROM spec_files WHERE project_id = ? AND root_path = ?", (project_id, root_path))}

    def file_status_counts(self, project_id: str, root_path: str) -> dict[str, int]:
        return {r["status"]: r["n"] for r in self._q(
            "SELECT status, COUNT(*) AS n FROM spec_files WHERE project_id = ? AND root_path = ? GROUP BY status",
            (project_id, root_path))}

    def upsert_file(self, project_id: str, root_path: str, rel_path: str, *, size: int, mtime_ns: int,
                    sha256: str | None, kind: str | None, status: str, reason: str | None,
                    document_id: str | None, created_document: bool) -> None:
        self.conn.execute(
            "INSERT INTO spec_files(project_id, root_path, rel_path, size_bytes, mtime_ns, sha256, kind, status, reason, "
            "document_id, created_document, scanned_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(project_id, root_path, rel_path) DO UPDATE SET size_bytes=excluded.size_bytes, "
            "mtime_ns=excluded.mtime_ns, sha256=excluded.sha256, kind=excluded.kind, status=excluded.status, "
            "reason=excluded.reason, document_id=excluded.document_id, created_document=excluded.created_document, "
            "scanned_at=excluded.scanned_at",
            (project_id, root_path, rel_path, size, mtime_ns, sha256, kind, status, reason, document_id,
             int(created_document), utcnow()))

    def delete_file(self, project_id: str, root_path: str, rel_path: str) -> None:
        self.conn.execute("DELETE FROM spec_files WHERE project_id = ? AND root_path = ? AND rel_path = ?",
                          (project_id, root_path, rel_path))

    def document_refs(self, project_id: str, document_id: str) -> int:
        return self._one("SELECT COUNT(*) AS n FROM spec_files WHERE project_id = ? AND document_id = ?",
                         (project_id, document_id))["n"]

    def document_by_sha(self, project_id: str, sha256: str) -> dict | None:
        return self._one("SELECT * FROM documents WHERE project_id = ? AND sha256 = ? ORDER BY imported_at LIMIT 1",
                         (project_id, sha256))

    # -- extraction -------------------------------------------------------------------------------------------------
    def documents_to_extract(self, project_id: str, version: int) -> list[dict]:
        return self._q(
            "SELECT d.* FROM documents d LEFT JOIN doc_extraction x ON x.document_id = d.id "
            "WHERE d.project_id = ? AND (x.document_id IS NULL OR x.extractor_version != ?) ORDER BY d.imported_at",
            (project_id, version))

    def document_chunks(self, document_id: str) -> list[EvidenceChunk]:
        rows = self.conn.execute("SELECT * FROM evidence WHERE document_id = ? ORDER BY COALESCE(page, 0), line_start",
                                 (document_id,))
        return [EvidenceChunk(id=r["id"], document_id=r["document_id"], filename=r["filename"], page=r["page"],
                              line_start=r["line_start"], line_end=r["line_end"], section=r["section"] or "",
                              text=r["text"], hash=r["hash"]) for r in rows]

    def save_extraction(self, project_id: str, document_id: str, version: int,
                        items: Iterable[tuple[str, RawCandidate]]) -> int:
        """Replace the candidates of one document (reviews are kept: they belong to the candidate ID, not the row)."""
        items = list(items)
        now = utcnow()
        with transaction(self.conn):
            self.conn.execute("DELETE FROM rule_candidates WHERE project_id = ? AND document_id = ?",
                              (project_id, document_id))
            self.conn.executemany(
                "INSERT OR REPLACE INTO rule_candidates(id, project_id, document_id, position, semantic_key, evidence_id, "
                "status, raw_json, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                ((cid, project_id, document_id, i, raw.semantic_key, raw.evidence_id, "NEEDS_REVIEW", _j(raw.to_dict()), now)
                 for i, (cid, raw) in enumerate(items)))
            self.conn.execute(
                "INSERT INTO doc_extraction(document_id, extractor_version, candidate_count, extracted_at) VALUES (?,?,?,?) "
                "ON CONFLICT(document_id) DO UPDATE SET extractor_version=excluded.extractor_version, "
                "candidate_count=excluded.candidate_count, extracted_at=excluded.extracted_at",
                (document_id, version, len(items), now))
        return len(items)

    def candidate_records(self, project_id: str) -> list[dict]:
        rows = self._q(
            "SELECT c.id, c.document_id, c.raw_json, c.status, d.sha256 AS doc_sha FROM rule_candidates c "
            "JOIN documents d ON d.id = c.document_id WHERE c.project_id = ? ORDER BY d.imported_at, d.id, c.position",
            (project_id,))
        return [{"id": r["id"], "document_id": r["document_id"], "doc_sha": r["doc_sha"], "stored_status": r["status"],
                 "raw": RawCandidate.from_dict(json.loads(r["raw_json"]))} for r in rows]

    def candidate_count(self, project_id: str) -> int:
        return self._one("SELECT COUNT(*) AS n FROM rule_candidates WHERE project_id = ?", (project_id,))["n"]

    def evidence_texts(self, project_id: str) -> dict[str, str]:
        return {r["id"]: r["text"] for r in self._q(
            "SELECT e.id, e.text FROM evidence e JOIN rule_candidates c ON c.document_id = e.document_id "
            "AND c.evidence_id = e.id WHERE c.project_id = ?", (project_id,))}

    def update_statuses(self, project_id: str, statuses: dict[str, str]) -> None:
        if not statuses:
            return
        with transaction(self.conn):
            self.conn.executemany("UPDATE rule_candidates SET status = ? WHERE project_id = ? AND id = ? AND status != ?",
                                  ((s, project_id, cid, s) for cid, s in statuses.items()))

    # -- review decisions --------------------------------------------------------------------------------------------
    def reviews(self, project_id: str) -> list[dict]:
        return [{"key": r["review_key"], "semantic_key": r["semantic_key"], "decision": json.loads(r["decision_json"])}
                for r in self._q("SELECT * FROM candidate_reviews WHERE project_id = ? ORDER BY updated_at", (project_id,))]

    def set_review(self, project_id: str, key: str, semantic_key: str | None, decision: dict | None) -> None:
        with transaction(self.conn):
            if not decision:
                self.conn.execute("DELETE FROM candidate_reviews WHERE project_id = ? AND review_key = ?", (project_id, key))
                return
            self.conn.execute(
                "INSERT INTO candidate_reviews(project_id, review_key, semantic_key, decision_json, updated_at) "
                "VALUES (?,?,?,?,?) ON CONFLICT(project_id, review_key) DO UPDATE SET semantic_key=excluded.semantic_key, "
                "decision_json=excluded.decision_json, updated_at=excluded.updated_at",
                (project_id, key, semantic_key, _j(decision), utcnow()))
