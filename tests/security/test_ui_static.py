"""The web page ships no external resources and nothing the CSP would have to excuse."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[2] / "src" / "app" / "static"
FILES = sorted(p for p in STATIC.rglob("*") if p.is_file())
TEXT = {p: p.read_text(encoding="utf-8") for p in FILES}


def test_ui_files_exist():
    assert {p.name for p in FILES} >= {"index.html", "app.css", "main.js", "viewer.js", "rules.js", "api.js", "util.js"}


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_no_external_urls_or_cdn(path):
    text = TEXT[path]
    assert not re.search(r"https?://", text), f"{path.name} refers to an external URL"
    assert not re.search(r"(?<!:)//cdn\.|//fonts\.|@import", text)
    assert "url(" not in text or "url(data:" in text


def test_index_has_no_inline_script_style_or_handlers():
    html = TEXT[STATIC / "index.html"]
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html)          # only external, same-origin scripts
    assert not re.search(r"<style\b", html)
    assert not re.search(r"\sstyle\s*=", html)
    assert not re.search(r"\son[a-z]+\s*=", html, re.I)
    assert not re.search(r"javascript:", html, re.I)
    assert re.findall(r'<script[^>]*src="([^"]+)"', html) == ["/static/js/main.js"]
    assert re.findall(r'<link[^>]*href="([^"]+)"', html) == ["data:,", "/static/app.css"]


@pytest.mark.parametrize("path", [p for p in FILES if p.suffix == ".js"], ids=lambda p: p.name)
def test_js_avoids_html_injection_and_dynamic_code(path):
    text = TEXT[path]
    for bad in (".innerHTML", ".outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function(",
                "setTimeout(\"", "setInterval(\"", "importScripts"):
        assert bad not in text, f"{path.name} uses {bad}"
    assert 'setAttribute("style"' not in text and "cssText" not in text      # blocked by the CSP anyway
    for m in re.finditer(r"""\.setAttribute\(\s*["']on""", text):
        raise AssertionError(f"{path.name} sets an event-handler attribute")


def test_the_page_sends_the_token_only_to_its_own_server():
    api = TEXT[STATIC / "js" / "api.js"]
    assert api.count("fetch(") == 1 and "xhr.open" in api
    assert "X-ERI-Token" in api and not re.search(r"fetch\(\s*[\"']http", api)
