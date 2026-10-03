"""``measure._containment`` skips building vertex lists when two bounding boxes are disjoint. That is only equivalent to the
full test while every vertex of an entity lies inside the entity's own bbox. This test pins that invariant for every
``make_*`` constructor (also after a ``to_dict``/``from_dict`` round trip, as the database does), so a new constructor or a
hand-set bbox that breaks it fails here first."""
from __future__ import annotations

import random

from core.models.entities import GeometryEntity, make_circle, make_polyline, make_text


def _inside(e: GeometryEntity) -> bool:
    b = e.bbox
    return all(b[0] <= v[0] <= b[2] and b[1] <= v[1] <= b[3] for v in e.vertices())


def _random_entities(rng: random.Random, n: int):
    for i in range(n):
        scale = rng.choice([1e-3, 1.0, 90.0, 5e3, 1e6])
        ox, oy = rng.uniform(-1e7, 1e7), rng.uniform(-1e7, 1e7)
        kind = rng.choice(["polyline", "closed", "circle", "text", "line", "single"])
        if kind in ("polyline", "closed"):
            pts = [(ox + rng.uniform(-scale, scale), oy + rng.uniform(-scale, scale)) for _ in range(rng.randint(2, 12))]
            yield make_polyline(f"e{i}", f"{i:X}", "t.dxf", "L", "LWPOLYLINE", pts, closed=kind == "closed")
        elif kind == "line":
            yield make_polyline(f"e{i}", f"{i:X}", "t.dxf", "L", "LINE", [(ox, oy), (ox + rng.uniform(-scale, scale), oy)])
        elif kind == "single":
            yield make_polyline(f"e{i}", f"{i:X}", "t.dxf", "L", "LWPOLYLINE", [(ox, oy)])
        elif kind == "circle":
            r = rng.choice([scale, -scale, scale * rng.random()])          # a negative radius is stored as abs()
            yield make_circle(f"e{i}", f"{i:X}", "t.dxf", "L", (ox, oy), r)
        else:
            yield make_text(f"e{i}", f"{i:X}", "t.dxf", "L", "TEXT", (ox, oy), rng.choice(["", "x", "SCADA tray 300"]),
                            rng.choice([0.0, 2.5, scale]))


def test_every_vertex_is_inside_its_own_bbox_for_all_constructors_and_after_a_round_trip():
    rng = random.Random(404)
    kinds = set()
    for e in _random_entities(rng, 3000):
        kinds.add((e.kind, e.closed))
        assert _inside(e), (e.kind, e.bbox, e.vertices()[:3])
        back = GeometryEntity.from_dict(e.to_dict())
        assert back.bbox == e.bbox and _inside(back), (e.kind, back.bbox)
    assert {("polyline", False), ("polyline", True), ("circle", True), ("text", False)} <= kinds


def test_circle_polygon_corners_never_leave_the_bbox_at_any_radius_or_position():
    rng = random.Random(1)
    for _ in range(1000):
        c = make_circle("c", "1", "t.dxf", "L", (rng.uniform(-1e9, 1e9), rng.uniform(-1e9, 1e9)), 10 ** rng.uniform(-6, 9))
        assert _inside(c)
