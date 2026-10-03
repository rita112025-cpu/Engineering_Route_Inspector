"""A rejected upload must be answered with its clear 4xx message and removed from disk, also on Windows.

Found by the full suite under ``-W error`` (intermittent, depending on garbage collection): when PyMuPDF failed to open
a damaged PDF from its path, the half-built Document stayed alive inside the exception traceback and kept the file
open, so deleting the rejected upload raised WinError 32 and the request ended as a 500 with a reset connection.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from importers.pdf import load_pdf
from importers.text import DocumentImportError


def test_damaged_pdf_leaves_no_open_file_behind_while_the_exception_is_still_alive(tmp_path):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"%PDF-junk")
    with pytest.raises(DocumentImportError) as info:           # `info` keeps the traceback (and every frame) alive
        load_pdf(bad, "doc_1", "h", "bad.pdf")
    bad.unlink()                                                # raised PermissionError (WinError 32) before the fix
    assert not bad.exists() and "PDF" in info.value.user_message


def test_empty_and_non_pdf_bytes_are_rejected_exactly_as_when_opening_by_path(tmp_path):
    for name, data in (("empty.pdf", b""), ("text.pdf", b"just text"), ("junk.pdf", b"%PDF-junk")):
        p = tmp_path / name
        p.write_bytes(data)
        with pytest.raises(DocumentImportError) as info:
            load_pdf(p, "d", "h", name)
        assert info.value.user_message == "無法讀取 PDF" and "損毀" in info.value.reason


def test_good_pdf_is_still_read_page_by_page(tmp_path):
    import fitz
    doc = fitz.open()
    for text in ("Section 1\nclearance 300 mm", "Section 2\nangle 5 deg"):
        doc.new_page().insert_text((72, 72), text)
    path = tmp_path / "ok.pdf"
    doc.save(str(path))
    doc.close()
    chunks, warnings = load_pdf(path, "d", "h", "ok.pdf")
    assert warnings == [] and {c.page for c in chunks} == {1, 2}
    assert any("300 mm" in c.text for c in chunks)
    path.unlink()                                               # the file is not held open after a successful read either


@pytest.mark.parametrize("name,data,code", [("bad.pdf", b"%PDF-junk", "DOCUMENT_UNREADABLE"), ("bad.docx", b"junk", "DOCUMENT_UNREADABLE")])
def test_a_failing_cleanup_does_not_turn_the_clear_answer_into_a_500(live, monkeypatch, name, data, code):
    pid = live.http.post("/api/projects", json={"name": "P"}).json()["id"]
    real_unlink = Path.unlink

    def locked(self, *a, **kw):                                  # as if another program held the rejected file open
        if self.name.endswith(name):
            raise PermissionError(32, "The process cannot access the file because it is being used by another process")
        return real_unlink(self, *a, **kw)
    monkeypatch.setattr(Path, "unlink", locked)
    r = live.http.post(f"/api/projects/{pid}/documents", files={"file": (name, data)})
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == code and "Traceback" not in r.text and "PermissionError" not in r.text


@pytest.mark.parametrize("name,data,code", [
    ("plan.dxf", b"AC1027" + b"\0" * 100, "DWG_UNSUPPORTED"),           # a DWG renamed to .dxf: the early "AC10" path
    ("bad.dxf", b"\x00\x01garbage", "DRAWING_UNREADABLE"),               # unreadable drawing: the cleanup in the except path
])
def test_a_failing_cleanup_of_a_rejected_drawing_does_not_become_a_500(live, monkeypatch, name, data, code):
    pid = live.http.post("/api/projects", json={"name": "P"}).json()["id"]
    real_unlink = Path.unlink

    def locked(self, *a, **kw):
        if self.name.endswith(name):
            raise PermissionError(32, "The process cannot access the file because it is being used by another process")
        return real_unlink(self, *a, **kw)
    monkeypatch.setattr(Path, "unlink", locked)
    r = live.http.post(f"/api/projects/{pid}/drawings", files={"file": (name, data)})
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == code and "Traceback" not in r.text and "PermissionError" not in r.text
    assert live.http.get("/api/health").status_code == 200              # the connection and the server are fine
