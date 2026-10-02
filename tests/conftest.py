"""Shared fixtures. Each test directory adds its own pytest marker."""
from __future__ import annotations

import re
import socket
import sys
import threading
import time
from pathlib import Path

import ezdxf
import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from core.models.entities import make_circle, make_polyline, make_text  # noqa: E402

_MARKERS = ("unit", "integration", "regression", "security", "performance", "browser")


def pytest_collection_modifyitems(config, items):
    tests_dir = Path(__file__).resolve().parent
    for item in items:
        try:
            parts = Path(str(item.fspath)).resolve().relative_to(tests_dir).parts[:-1]   # folders below tests/
        except ValueError:
            continue
        for name in _MARKERS:
            if name in parts:
                item.add_marker(getattr(pytest.mark, name))


class EntityFactory:
    """Builds GeometryEntity objects with sequential handles."""

    def __init__(self, source: str = "test.dxf"):
        self.source = source
        self.n = 0

    def _next(self) -> tuple[str, str]:
        self.n += 1
        h = f"{self.n:X}"
        return f"{self.source}#{h}", h

    def line(self, layer, *pts, closed=False, entity_type="LWPOLYLINE", **md):
        eid, h = self._next()
        return make_polyline(eid, h, self.source, layer, entity_type, list(pts), closed, md)

    def rect(self, layer, x0, y0, x1, y1, **md):
        return self.line(layer, (x0, y0), (x1, y0), (x1, y1), (x0, y1), closed=True, **md)

    def circle(self, layer, center, r, **md):
        eid, h = self._next()
        return make_circle(eid, h, self.source, layer, center, r, md)

    def text(self, layer, pos, text, height=100.0, **md):
        eid, h = self._next()
        return make_text(eid, h, self.source, layer, "TEXT", pos, text, height, md)


@pytest.fixture
def ef():
    return EntityFactory()


# -- a real server on a real socket ---------------------------------------------------------------------

INDEX_HTML = '<!doctype html><meta name="eri-token" content="__ERI_TOKEN__"><title>test</title><p>ok</p>'


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LiveServer:
    """The real application served by uvicorn on 127.0.0.1, with a token-aware httpx client.

    ``real_ui=True`` serves the application's own web page instead of a tiny test page.
    """

    def __init__(self, tmp_path: Path, real_ui: bool = False, **config_overrides):
        import uvicorn
        from app.config import AppConfig
        from app.server import create_app
        self.port = free_port()
        if not real_ui:
            static = tmp_path / "static"
            static.mkdir(parents=True, exist_ok=True)
            (static / "index.html").write_text(INDEX_HTML, encoding="utf-8")
            (static / "app.js").write_text("console.log('x')", encoding="utf-8")
            config_overrides["static_dir"] = static
        self.config = AppConfig(data_dir=tmp_path / "data", port=self.port, **config_overrides)
        self.app = create_app(self.config)
        self.server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="error",
                                                    lifespan="on"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 20
        while not self.server.started:
            if time.monotonic() > deadline or not self.thread.is_alive():
                raise RuntimeError("test server did not start")
            time.sleep(0.02)
        self.base = f"http://127.0.0.1:{self.port}"
        self.raw = httpx.Client(base_url=self.base, timeout=60)           # no token
        html = self.raw.get("/").text
        self.token = re.search(r'name="eri-token" content="([^"]+)"', html).group(1)
        self.http = httpx.Client(base_url=self.base, timeout=60, headers={"X-ERI-Token": self.token})

    @property
    def state(self):
        return self.app.state.eri

    def close(self):
        self.http.close()
        self.raw.close()
        self.server.should_exit = True
        self.thread.join(20)


@pytest.fixture
def live(tmp_path):
    srv = LiveServer(tmp_path)
    yield srv
    srv.close()


@pytest.fixture
def live_factory(tmp_path):
    made = []

    def make(**overrides):
        srv = LiveServer(tmp_path / f"s{len(made)}", **overrides)
        made.append(srv)
        return srv
    yield make
    for srv in made:
        srv.close()



def write_sample_dxf(path: Path) -> Path:
    """Small real drawing: one FAIL, one WARNING, one PASS, one UNKNOWN (crossing without Z)."""
    doc = ezdxf.new("R2018")
    doc.header["$INSUNITS"] = 4
    msp = doc.modelspace()
    scada = "SCADA-CABLE"
    for y, gap in ((0, 250), (2000, 320), (4000, 500)):          # FAIL, WARNING, PASS
        msp.add_line((0, y), (5000, y), dxfattribs={"layer": scada})
        msp.add_line((0, y + gap), (5000, y + gap), dxfattribs={"layer": "POWER-CABLE"})
    msp.add_line((7000, 0), (7000, 1000), dxfattribs={"layer": scada})                  # crossing -> UNKNOWN
    msp.add_line((6500, 500), (7500, 500), dxfattribs={"layer": "POWER-CABLE"})
    doc.saveas(path)
    return path


@pytest.fixture
def sample_dxf(tmp_path):
    return write_sample_dxf(tmp_path / "plan.dxf")
