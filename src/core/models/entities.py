"""Parser-independent internal geometry model.

Nothing in ``core`` imports ezdxf: importers convert CAD objects into
``GeometryEntity`` instances and the engine only ever sees these.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

Point = tuple[float, float]
BBox = tuple[float, float, float, float]  # minx, miny, maxx, maxy

GEOMETRIC_KINDS = ("polyline", "circle")
CIRCLE_SEGMENTS = 48


@dataclass
class GeometryEntity:
    """One normalized drawing object.

    geometry is one of:
      {"kind": "polyline", "points": [[x, y], ...], "closed": bool}
      {"kind": "circle", "center": [x, y], "radius": r}
      {"kind": "text", "position": [x, y], "text": str, "height": h}
    """

    id: str
    handle: str
    source_file: str
    layer: str
    entity_type: str
    geometry: dict[str, Any]
    bbox: BBox
    metadata: dict[str, Any] = field(default_factory=dict)
    _segments: list | None = field(default=None, repr=False, compare=False)

    @property
    def kind(self) -> str:
        return self.geometry["kind"]

    @property
    def is_geometric(self) -> bool:
        return self.kind in GEOMETRIC_KINDS

    @property
    def closed(self) -> bool:
        if self.kind == "circle":
            return True
        return bool(self.geometry.get("closed", False))

    def vertices(self) -> list[Point]:
        g = self.geometry
        if g["kind"] == "polyline":
            return [(float(p[0]), float(p[1])) for p in g["points"]]
        if g["kind"] == "circle":
            cx, cy = g["center"]
            r = g["radius"]
            return [
                (cx + r * math.cos(2 * math.pi * i / CIRCLE_SEGMENTS),
                 cy + r * math.sin(2 * math.pi * i / CIRCLE_SEGMENTS))
                for i in range(CIRCLE_SEGMENTS)
            ]
        x, y = g["position"]
        return [(float(x), float(y))]

    def segments(self) -> list[tuple[Point, Point]]:
        if self._segments is None:
            pts = self.vertices()
            segs: list[tuple[Point, Point]] = []
            if len(pts) == 1:
                segs.append((pts[0], pts[0]))
            else:
                for a, b in zip(pts, pts[1:]):
                    segs.append((a, b))
                if self.closed and len(pts) > 2 and pts[0] != pts[-1]:
                    segs.append((pts[-1], pts[0]))
            self._segments = segs
        return self._segments

    def length(self) -> float:
        if self.kind == "circle":
            return 2 * math.pi * self.geometry["radius"]
        if self.kind == "text":
            return 0.0
        return sum(math.dist(a, b) for a, b in self.segments())

    def center(self) -> Point:
        x0, y0, x1, y1 = self.bbox
        return ((x0 + x1) / 2, (y0 + y1) / 2)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "handle": self.handle,
            "source_file": self.source_file,
            "layer": self.layer,
            "entity_type": self.entity_type,
            "geometry": self.geometry,
            "bbox": list(self.bbox),
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "GeometryEntity":
        return cls(
            id=d["id"], handle=d["handle"], source_file=d["source_file"],
            layer=d["layer"], entity_type=d["entity_type"], geometry=d["geometry"],
            bbox=tuple(d["bbox"]), metadata=dict(d.get("metadata") or {}),
        )


def bbox_of_points(points: list[Point]) -> BBox:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return (min(xs), min(ys), max(xs), max(ys))


def make_polyline(id: str, handle: str, source_file: str, layer: str, entity_type: str,
                  points: list[Point], closed: bool = False,
                  metadata: dict | None = None) -> GeometryEntity:
    pts = [(float(x), float(y)) for x, y in points]
    return GeometryEntity(
        id=id, handle=handle, source_file=source_file, layer=layer, entity_type=entity_type,
        geometry={"kind": "polyline", "points": [list(p) for p in pts], "closed": closed},
        bbox=bbox_of_points(pts), metadata=dict(metadata or {}),
    )


def make_circle(id: str, handle: str, source_file: str, layer: str, center: Point, radius: float,
                metadata: dict | None = None, entity_type: str = "CIRCLE") -> GeometryEntity:
    cx, cy = center
    r = abs(radius)
    return GeometryEntity(
        id=id, handle=handle, source_file=source_file, layer=layer, entity_type=entity_type,
        geometry={"kind": "circle", "center": [cx, cy], "radius": r},
        bbox=(cx - r, cy - r, cx + r, cy + r), metadata=dict(metadata or {}),
    )


def make_text(id: str, handle: str, source_file: str, layer: str, entity_type: str,
              position: Point, text: str, height: float,
              metadata: dict | None = None) -> GeometryEntity:
    x, y = position
    h = abs(height) or 1.0
    w = max(len(text), 1) * h * 0.6
    return GeometryEntity(
        id=id, handle=handle, source_file=source_file, layer=layer, entity_type=entity_type,
        geometry={"kind": "text", "position": [x, y], "text": text, "height": h},
        bbox=(x, y, x + w, y + h), metadata=dict(metadata or {}),
    )
