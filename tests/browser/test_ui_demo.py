"""What a first-time user does: open the page, load the demo, press Analyse, click a red issue, export."""
from __future__ import annotations

import pytest

from .conftest import shot
from .test_ui_flow import canvas_probe, wait_idle


def test_first_time_user_with_the_demo(ui, tmp_path):
    page, srv, errors = ui
    page.goto(srv.base + "/")
    page.wait_for_selector("#project-select option", state="attached")
    assert "載入示範專案" in page.inner_text("#viewer-empty")            # the empty page points at the demo
    page.click("#demo-load")
    page.wait_for_function("() => document.querySelector('#project-select option:checked').textContent.includes('示範專案')")
    page.wait_for_selector("#drawing-list li")
    assert page.inner_text("#drawing-list li .name") == "demo_plan.dxf"
    assert page.inner_text("#doc-list li .name") == "demo_spec.md"
    assert "5 條規則" in page.inner_text("#rules-summary-text")
    page.wait_for_function("() => window.eri.viewer.hasDrawing()")
    assert canvas_probe(page)["nonBg"] > 500
    assert page.is_enabled("#run-btn")
    shot(page, "demo-1-loaded")

    page.click("#run-btn")
    wait_idle(page)
    page.wait_for_selector(".issue")
    chips = {c.get_attribute("data-status"): int(c.inner_text().split("\n")[0]) for c in page.locator(".chip").all()}
    assert chips == {"FAIL": 6, "WARNING": 1, "UNKNOWN": 2, "PASS": 26}      # all four outcomes
    assert all(v > 0 for v in chips.values())
    assert page.locator('.issue[data-status="FAIL"]').count() == 6
    shot(page, "demo-2-results")

    # click a red issue: located on the drawing, and every field is explained
    row = page.locator('.issue[data-status="FAIL"]', has_text="進入").first
    row.click()
    page.wait_for_function("() => window.eri.app.detail && window.eri.app.detail.rule_id.startsWith('KEEP-OUT')")
    detail = page.inner_text("#detail")
    for needle in ("Handle", "圖層 Layer", "SCADA-CABLE", "實測值 Measured", "進入了區域", "規則要求 Required",
                   "必須在區域外", "規則 Rule", "KEEP-OUT-01", "規範證據 Evidence", "demo_spec.md", "機房禁設區"):
        assert needle in detail, (needle, detail)
    assert page.evaluate("() => window.eri.viewer.selected.entities.length") >= 2
    assert canvas_probe(page)["reds"] > 20
    shot(page, "demo-3-detail")

    # the yes/no flags of zone and crossing rules never show as a bare 0 or 1
    page.click('.chip[data-status="PASS"]')                              # show the passing items too
    page.wait_for_function("() => document.querySelectorAll('.issue').length > 9")
    texts = page.locator(".issue .sub").all_inner_texts()
    assert not any("實測 1 ·" in t or "實測 0 ·" in t or t.endswith("實測 1") for t in texts), texts

    for fmt, suffix in (("csv", ".csv"), ("markdown", ".md"), ("html", ".html"), ("dxf", ".dxf")):
        with page.expect_download() as dl:
            page.click(f'[data-export="{fmt}"]')
        name = dl.value.suggested_filename
        assert name.endswith(suffix) and name.startswith("demo_plan"), name
        dl.value.save_as(tmp_path / name)
    csv_text = (tmp_path / next(p.name for p in tmp_path.iterdir() if p.suffix == ".csv")).read_text(encoding="utf-8-sig")
    assert csv_text.count("\n") >= 36 and "不合格" in csv_text and "無法判定" in csv_text and "警告" in csv_text
    assert errors == [], errors


def test_loading_the_demo_twice_just_switches(ui):
    page, srv, errors = ui
    page.goto(srv.base + "/")
    page.wait_for_selector("#project-select option", state="attached")
    page.click("#demo-load")
    page.wait_for_selector("#drawing-list li")
    page.click("#demo-load")
    page.wait_for_function("() => [...document.querySelectorAll('.toast .msg')].some(t => t.textContent.includes('已經存在'))")
    assert page.locator("#project-select option").count() == 2          # "my project" + one demo, never two demos
    assert errors == [], errors
