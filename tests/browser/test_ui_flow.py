"""The whole product, operated through a real browser:
drag the drawing -> drag the specification -> build a rule -> start analysis -> click a red issue
-> read why -> export. Plus the error paths a first-time user hits.
"""
from __future__ import annotations

import base64
import csv
import io

import pytest

from .conftest import shot

SPEC = ("# 弱電管線規範\n\n4.2 間距\nSCADA 與電力電纜之水平淨距不得小於 300 mm，警示範圍 50 mm。\n\n"
        "4.3 禁設區\n電纜不得穿越機房禁設區。\n")


def drop_file(page, zone: str, name: str, data: bytes) -> None:
    """A genuine drop event carrying a File, as when a user drags a file from the desktop."""
    b64 = base64.b64encode(data).decode()
    handle = page.evaluate_handle(
        """([name, b64]) => { const dt = new DataTransfer();
             dt.items.add(new File([Uint8Array.from(atob(b64), c => c.charCodeAt(0))], name)); return dt; }""",
        [name, b64])
    page.dispatch_event(zone, "dragenter", {"dataTransfer": handle})
    page.dispatch_event(zone, "drop", {"dataTransfer": handle})


def toast_texts(page) -> list[str]:
    return [t.inner_text() for t in page.locator(".toast .msg").all()]


def wait_idle(page, timeout=60000):
    page.wait_for_function("() => window.eri && window.eri.app.run && ['completed','failed','cancelled'].includes(window.eri.app.run.status)", timeout=timeout)


def canvas_probe(page):
    return page.evaluate("() => window.eri.viewer.probe()")


@pytest.fixture
def loaded(ui, sample_dxf):
    page, srv, errors = ui
    page.goto(srv.base + "/")
    page.wait_for_selector("#project-select option", state="attached")
    drop_file(page, "#drop-drawing", "plan.dxf", sample_dxf.read_bytes())
    page.wait_for_selector("#drawing-list li")
    drop_file(page, "#drop-spec", "spec.md", SPEC.encode("utf-8"))
    page.wait_for_selector("#doc-list li")
    return page, srv, errors


def build_clearance_rule(page):
    """Rule Builder: group layers, drag the specification paragraph onto 'new rule', add, save."""
    page.click("#rules-open")
    page.wait_for_selector("#rules-dialog[open]")
    page.click("#rb-auto")
    names = page.locator(".rb-sys input[type=text]").evaluate_all("els => els.map(e => e.value)")
    assert sorted(names) == ["POWER", "SCADA"]
    page.select_option("#rb-template", "clearance")
    page.select_option('[data-blank="subject"]', "system:SCADA")
    page.select_option('[data-blank="target"]', "system:POWER")
    # drag the paragraph that talks about clearance onto the "new rule" box (real HTML5 drag and drop)
    para = page.locator(".para", has_text="水平淨距").first
    para.drag_to(page.locator("#rb-new"))
    assert page.input_value('[data-blank="value"]') == "300"
    assert page.input_value('[data-blank="unit"]') == "mm"
    assert "spec.md" in page.inner_text("#rb-new-evi")
    page.fill('[data-blank="warn_margin"]', "50")
    page.click("#rb-add-rule")
    page.wait_for_selector(".rb-card")
    assert "不得小於 300 mm" in page.inner_text(".rb-sentence")
    assert "spec.md" in page.inner_text(".rb-card .rb-evi")
    shot(page, "rules-dialog")
    page.click("#rb-save")
    page.wait_for_selector("#rules-dialog", state="hidden")
    page.wait_for_function("() => document.querySelector('#rules-summary-text').textContent.includes('1 條')")


def test_first_run_experience_end_to_end(loaded, tmp_path):
    page, srv, errors = loaded
    # -- the drawing is on screen, nothing analysed yet
    page.wait_for_function("() => window.eri.viewer.hasDrawing()")
    assert page.inner_text("#project-select option:checked") == "我的專案"
    assert canvas_probe(page)["nonBg"] > 200
    assert page.is_disabled("#run-btn") and "規則" in page.inner_text("#run-hint")
    shot(page, "1-drawing-loaded")

    build_clearance_rule(page)
    shot(page, "2-rules-saved")
    assert page.is_enabled("#run-btn")

    # -- start the analysis
    page.click("#run-btn")
    wait_idle(page)
    assert page.evaluate("() => window.eri.app.run.status") == "completed"
    page.wait_for_selector(".issue")
    chips = {c.get_attribute("data-status"): c.inner_text().split("\n")[0]
             for c in page.locator(".chip").all()}
    assert chips == {"FAIL": "1", "WARNING": "1", "UNKNOWN": "1", "PASS": "1"}
    assert page.locator(".issue").count() == 3                       # PASS is hidden until asked for
    page.click('.chip[data-status="PASS"]')
    page.wait_for_function("() => document.querySelectorAll('.issue').length === 4")
    page.click('.chip[data-status="PASS"]')
    page.wait_for_function("() => document.querySelectorAll('.issue').length === 3")

    # -- the red issue is selected and explained, and located on the drawing
    sel = page.locator('.issue[aria-selected="true"]')
    assert sel.get_attribute("data-status") == "FAIL"
    detail = page.inner_text("#detail")
    for needle in ("Handle", "圖層 Layer", "SCADA-CABLE", "實測值 Measured", "250 mm", "規則要求 Required",
                   ">= 300 mm", "規則 Rule", "規範證據 Evidence", "spec.md", "不得小於 300 mm", "建議處理", "已確認"):
        assert needle in detail, (needle, detail)
    probe = canvas_probe(page)
    assert probe["reds"] > 20, probe                                   # the red marker / highlight is drawn
    shot(page, "3-results")

    # -- clicking a marker on the drawing selects its issue
    other = page.evaluate("""() => { const v = window.eri.viewer; const i = v.issues.find(x => x.status === 'WARNING');
        const [sx, sy] = v.toScreen(i.location[0], i.location[1]); const r = v.canvas.getBoundingClientRect();
        return {id: i.issue_id, x: r.left + sx, y: r.top + sy}; }""")
    page.mouse.click(other["x"], other["y"])
    page.wait_for_function("(id) => window.eri.app.selectedId === id", arg=other["id"])
    assert "警告" in page.inner_text("#detail")

    # -- keyboard: arrow keys walk the list
    page.focus("#issue-list")
    page.keyboard.press("ArrowDown")
    page.wait_for_function("(id) => window.eri.app.selectedId !== id", arg=other["id"])

    # -- unknown results explain themselves instead of pretending
    page.click('.issue[data-status="UNKNOWN"]')
    page.wait_for_function("() => window.eri.app.detail && window.eri.app.detail.status === 'UNKNOWN'")
    assert "無法量測" in page.inner_text("#detail") and "高程" in page.inner_text("#detail")

    # -- every export arrives as a download
    results = {}
    for fmt, suffix in (("csv", ".csv"), ("markdown", ".md"), ("html", ".html"), ("dxf", ".dxf")):
        with page.expect_download() as dl:
            page.click(f'[data-export="{fmt}"]')
        d = dl.value
        assert d.suggested_filename.endswith(suffix), d.suggested_filename
        target = tmp_path / d.suggested_filename
        d.save_as(target)
        results[fmt] = target.read_bytes()
        assert "已儲存" in page.inner_text("#export-status")
    rows = list(csv.reader(io.StringIO(results["csv"].decode("utf-8-sig"))))
    assert len(rows) == 5 and rows[0][:2] == ["問題編號", "狀態"]
    assert "# 工程管線檢查報告" in results["markdown"].decode()
    assert b"default-src 'none'" in results["html"] and b"ERI_FAIL" in results["dxf"]

    # -- a reload shows the same project, drawing and results again
    page.reload()
    page.wait_for_selector(".issue")
    assert page.locator(".issue").count() == 3
    assert page.inner_text("#project-select option:checked") == "我的專案"
    assert canvas_probe(page)["reds"] > 20

    assert errors == [], errors          # no console errors, no CSP violations, no failed requests


def test_cancel_button_stops_a_running_analysis(ui):
    page, srv, errors = ui
    import ezdxf
    doc = ezdxf.new("R2018")
    doc.modelspace().add_text("a" * 40 + "!", dxfattribs={"layer": "NOTE"})
    buf = io.StringIO()
    doc.write(buf)
    page.goto(srv.base + "/")
    page.wait_for_selector("#project-select option", state="attached")
    drop_file(page, "#drop-drawing", "text.dxf", buf.getvalue().encode())
    page.wait_for_selector("#drawing-list li")
    pid = page.evaluate("() => window.eri.app.project.id")
    hang = {"systems": [], "rules": [{"id": "HANG", "name": "hang", "subject": {"entity_type": "TEXT", "text_regex": "^(a|aa)+$"},
                                       "measurement": "entity_count", "operator": ">=", "value": 1}]}
    srv.state.db.repo().save_ruleset(pid, hang)      # the API itself refuses this pattern (see the security tests)
    page.reload()
    page.wait_for_selector("#drawing-list li")
    page.wait_for_function("() => !document.querySelector('#run-btn').disabled")
    page.click("#run-btn")
    page.wait_for_selector("#progress-box:not([hidden])")
    page.wait_for_function("() => document.querySelector('#progress-label').textContent.includes('執行規則')")
    page.click("#cancel-btn")
    wait_idle(page, 40000)
    assert page.evaluate("() => window.eri.app.run.status") == "cancelled"
    page.wait_for_function("() => document.querySelector('#run-btn') && !document.querySelector('#run-btn').disabled")
    assert any("已取消" in t for t in toast_texts(page))


def test_unsupported_files_get_clear_messages(ui):
    page, srv, errors = ui
    page.goto(srv.base + "/")
    page.wait_for_selector("#project-select option", state="attached")
    drop_file(page, "#drop-drawing", "plan.dwg", b"AC1027")
    page.wait_for_selector(".toast.error")
    assert "DWG" in page.inner_text(".toast.error") and "DXF" in page.inner_text(".toast.error")
    drop_file(page, "#drop-spec", "notes.exe", b"MZ")
    page.wait_for_function("() => document.querySelectorAll('.toast.error').length >= 2")
    assert any("不支援的檔案" in t for t in toast_texts(page))
    drop_file(page, "#drop-drawing", "broken.dxf", b"\x00\x01garbage")
    page.wait_for_function("() => document.querySelectorAll('.toast.error').length >= 3")
    assert any("無法讀取 DXF" in t for t in toast_texts(page))
    assert page.locator("#drawing-list li").count() == 0
    assert not any("Traceback" in t for t in toast_texts(page))


def test_project_management_and_dialogs(ui):
    page, srv, errors = ui
    page.goto(srv.base + "/")
    page.wait_for_selector("#project-select option", state="attached")
    page.click("#project-new")
    page.fill("#ask-input", "第二個專案")
    page.click("#ask-ok")
    page.wait_for_function("() => [...document.querySelectorAll('#project-select option')].length === 2")
    assert page.inner_text("#project-select option:checked") == "第二個專案"
    page.click("#project-rename")
    page.fill("#ask-input", "改過的名字")
    page.click("#ask-ok")
    page.wait_for_function("() => document.querySelector('#project-select option:checked').textContent === '改過的名字'")
    page.click("#project-delete")
    page.wait_for_selector("#ask-dialog[open]")
    page.click("#ask-cancel")                                      # cancelling keeps the project
    assert page.locator("#project-select option").count() == 2
    page.click("#project-delete")
    page.click("#ask-ok")
    page.wait_for_function("() => document.querySelectorAll('#project-select option').length === 1")
    assert page.inner_text("#project-select option:checked") == "我的專案"
    assert errors == [], errors


def test_rule_builder_validation_and_editing(loaded):
    page, srv, errors = loaded
    page.click("#rules-open")
    page.wait_for_selector("#rules-dialog[open]")
    page.click("#rb-add-rule")                                      # nothing chosen yet: a readable complaint
    page.wait_for_function("() => document.querySelector('#rb-form-errors').textContent.includes('請選擇')")
    page.click("#rb-auto")
    page.select_option("#rb-template", "no_crossing")
    page.select_option('[data-blank="subject"]', "system:SCADA")
    page.select_option('[data-blank="target"]', "system:POWER")
    page.click("#rb-add-rule")
    page.wait_for_selector(".rb-card")
    assert "不得與" in page.inner_text(".rb-sentence")
    page.click(".rb-card >> text=編輯")                              # reopen with the same blanks
    assert page.input_value('[data-blank="subject"]') == "system:SCADA"
    page.select_option("#rb-template", "clearance")
    page.select_option('[data-blank="subject"]', "system:SCADA")
    page.select_option('[data-blank="target"]', "system:POWER")
    page.fill('[data-blank="value"]', "400")
    page.click("#rb-add-rule")
    page.wait_for_function("() => document.querySelectorAll('.rb-card').length >= 1")
    # unsaved changes: closing asks first
    page.click("#rules-close")
    page.wait_for_selector("#ask-dialog[open]")
    page.click("#ask-cancel")
    assert page.locator("#rules-dialog[open]").count() == 1
    page.click("#rules-close")
    page.click("#ask-ok")
    page.wait_for_selector("#rules-dialog", state="hidden")
    assert "尚未建立規則" in page.inner_text("#rules-summary-text")     # nothing was saved
    # the only console error is the 400 this test provoked on purpose (the empty form); nothing else
    assert [e for e in errors if "status of 400" not in e] == [], errors
    assert sum("status of 400" in e for e in errors) == 1, errors


def test_layout_holds_at_other_sizes(loaded):
    page, srv, errors = loaded
    for width, height, name in ((1024, 768, "narrow"), (390, 844, "phone")):
        page.set_viewport_size({"width": width, "height": height})
        page.wait_for_timeout(150)
        overflow = page.evaluate("() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
        assert overflow <= 1, (name, overflow)                       # no sideways scrolling
        assert page.is_visible("#drop-drawing") and page.is_visible("#viewer")
        shot(page, f"layout-{name}")
    assert errors == [], errors
