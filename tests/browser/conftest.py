"""Browser tests: a real headless Chromium (Playwright) against the real server and the real web page.

They are skipped when Playwright or a Chromium build is not available, so the suite still runs on a
machine without a browser. Nothing here is a human acceptance test: see the README for what a person
still has to try.
"""
from __future__ import annotations

import glob
import os
from pathlib import Path

import pytest

from tests.conftest import LiveServer

SHOTS = os.environ.get("ERI_SCREENSHOT_DIR")


def chromium_path() -> str | None:
    env = os.environ.get("ERI_CHROMIUM")
    cands = [env] if env else []
    cands += ["/opt/pw-browsers/chromium", *glob.glob("/opt/pw-browsers/chromium-*/chrome-linux*/chrome"),
              "/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/google-chrome"]
    return next((c for c in cands if c and Path(c).exists()), None)


@pytest.fixture(scope="session")
def playwright_instance():
    sync = pytest.importorskip("playwright.sync_api")
    with sync.sync_playwright() as p:
        yield p


@pytest.fixture(scope="session")
def browser(playwright_instance):
    path = chromium_path()
    try:
        b = playwright_instance.chromium.launch(executable_path=path, args=["--no-sandbox"]) if path \
            else playwright_instance.chromium.launch(args=["--no-sandbox"])
    except Exception as exc:  # noqa: BLE001 - no usable browser on this machine
        pytest.skip(f"no Chromium available: {exc}")
    yield b
    b.close()


@pytest.fixture
def ui(tmp_path, browser):
    """(page, server, errors): errors collects console errors, page errors and failed requests."""
    srv = LiveServer(tmp_path, real_ui=True)
    ctx = browser.new_context(viewport={"width": 1440, "height": 900}, accept_downloads=True)
    ctx.set_default_timeout(20000)
    page = ctx.new_page()
    errors: list[str] = []
    page.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}") if m.type in ("error", "warning") else None)
    page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    page.on("requestfailed", lambda r: errors.append(f"requestfailed: {r.url} {r.failure}"))
    page.on("response", lambda r: errors.append(f"http {r.status}: {r.url}") if r.status >= 500 else None)
    yield page, srv, errors
    ctx.close()
    srv.close()


def shot(page, name: str) -> None:
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(SHOTS) / f"{name}.png"))
