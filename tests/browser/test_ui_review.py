"""Specification scan and rule review, operated through a real browser: scan -> read the four kinds of card ->
decide only the doubtful ones -> the analysis button follows what will really run."""
from __future__ import annotations

from pathlib import Path

import pytest

from .conftest import shot
from .test_ui_flow import drop_file, wait_idle

CONFIRMED_ONLY = "SCADA 線纜架與電力線纜架之水平淨距不得小於 300 mm。\n"
REVIEW_ONLY = "通信線纜架與電力線纜架之水平淨距不得小於 300 mm。\n"
MIXED = (CONFIRMED_ONLY + REVIEW_ONLY + "管線應保持適當距離。\nSCADA 線纜架與給水管之水平淨距不得小於 300 mm。\n"
         "SCADA 線纜架與給水管之水平淨距不得小於 500 mm。\n")


def start(ui, sample_dxf, tmp_path: Path, text: str | None, glossary: str = ""):
    page, srv, errors = ui
    page.goto(srv.base + "/")
    page.wait_for_selector("#project-select option", state="attached")
    drop_file(page, "#drop-drawing", "plan.dxf", sample_dxf.read_bytes())
    page.wait_for_selector("#drawing-list li")
    if text is not None:
        folder = tmp_path / "規範"
        folder.mkdir(exist_ok=True)
        (folder / "A.md").write_text(text, encoding="utf-8")
        pid = page.evaluate("() => window.eri.app.project.id")
        r = srv.http.put(f"/api/projects/{pid}/spec-source", json={"path": str(folder), "glossary_text": glossary})
        assert r.status_code == 200, r.text
        page.reload()
        page.wait_for_selector("#drawing-list li")
        page.click("#spec-scan")
        page.wait_for_function("() => document.querySelector('#spec-status').textContent.includes('上次掃描')")
    return page, srv, errors


def counts(page) -> list[int]:
    return [int(page.inner_text(f"#cnt-{k}")) for k in ("usable", "review", "conflict", "not_enforceable")]


def test_no_rules_means_no_analysis(ui, sample_dxf, tmp_path):
    page, srv, errors = start(ui, sample_dxf, tmp_path, None)
    assert page.is_disabled("#run-btn") and "掃描規範" in page.inner_text("#run-hint")
    assert page.is_disabled("#review-open")
    assert counts(page) == [0, 0, 0, 0]


def test_only_unconfirmed_rules_do_not_enable_the_analysis(ui, sample_dxf, tmp_path):
    page, srv, errors = start(ui, sample_dxf, tmp_path, REVIEW_ONLY)
    assert counts(page) == [0, 1, 0, 0]
    assert page.is_disabled("#run-btn")


def test_a_confirmed_rule_enables_the_analysis_and_unresolved_ones_do_not_block_it(ui, sample_dxf, tmp_path):
    page, srv, errors = start(ui, sample_dxf, tmp_path, MIXED, "給水管 = WATER-PIPE")
    assert counts(page)[0] == 1 and counts(page)[1] == 1 and counts(page)[3] == 1
    assert page.is_enabled("#run-btn")
    hint = page.inner_text("#run-hint")
    assert "1 條規則會執行" in hint and "尚未確認" in hint and "本次未確認規則不會執行" in hint
    shot(page, "review-0-summary")

    page.click("#review-open")
    page.wait_for_selector("#review-dialog[open] .rv-card")
    statuses = {c.get_attribute("data-status") for c in page.locator(".rv-card").all()}
    assert statuses >= {"CONFIRMED", "NEEDS_REVIEW", "NOT_ENFORCEABLE"}             # every kind of card renders
    assert "CONFIRMED" in page.inner_text(".rv-card.CONFIRMED .rv-badge")
    assert "A.md" in page.inner_text(".rv-card.CONFIRMED .rv-src")
    page.click(".rv-card.CONFIRMED >> text=查看原文")
    assert "不得小於 300 mm" in page.inner_text(".rv-card.CONFIRMED .rv-evi q")
    assert "無法由 2D" in page.inner_text(".rv-card.NOT_ENFORCEABLE") or "無法自動判定" in page.inner_text(".rv-card.NOT_ENFORCEABLE")
    shot(page, "review-1-cards")

    # filter, search
    page.click('#rv-tabs [data-bucket="review"]')
    page.wait_for_function("() => [...document.querySelectorAll('.rv-card')].every(c => c.dataset.status === 'NEEDS_REVIEW')")
    page.fill("#rv-search", "不存在的字")
    page.wait_for_selector(".rv-empty")
    page.fill("#rv-search", "")
    page.click('#rv-tabs [data-bucket="all"]')

    # decide the doubtful one: name the missing object
    page.select_option('.rv-card.NEEDS_REVIEW select[data-slot="subject"]', "layer:SCADA-CABLE")
    page.wait_for_function("() => !document.querySelector('.rv-card.NEEDS_REVIEW')")
    # nothing else asks for anything except the conflict, which is never decided for the user
    assert page.locator(".rv-card.CONFLICTING").count() == 1
    page.click('.rv-card.CONFLICTING button[data-choose="300"]')
    page.wait_for_function("() => !document.querySelector('.rv-card.CONFLICTING.bucket-conflict')")

    # start from the dialog
    page.click("#rv-run")
    wait_idle(page)
    assert page.evaluate("() => window.eri.app.run.status") == "completed"
    page.wait_for_selector(".issue")
    page.locator('.issue[data-status="FAIL"]').first.click()
    page.wait_for_function("() => window.eri.app.detail && window.eri.app.detail.evidence")
    detail = page.inner_text("#detail")
    for needle in ("實測值 Measured", "規則要求 Required", "規則 Rule", "規範證據 Evidence", "A.md"):
        assert needle in detail, needle
    assert errors == [], errors


def test_the_manual_rule_editor_is_still_there_under_advanced_settings(ui, sample_dxf, tmp_path):
    page, srv, errors = start(ui, sample_dxf, tmp_path, None)
    assert not page.locator("#rules-open").is_visible()                    # tucked away, not the main entrance
    page.click("#adv-settings summary")
    page.click("#rules-open")
    page.wait_for_selector("#rules-dialog[open]")
    assert page.locator("#rb-auto").count() == 1 and page.locator("#rb-template").count() == 1
