"""Shared fixtures. Each test directory adds its own pytest marker."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from core.models.entities import make_circle, make_polyline, make_text  # noqa: E402

_MARKERS = ("unit", "integration", "regression", "security", "performance")


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
