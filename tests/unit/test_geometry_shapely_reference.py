"""Cross-check of ``core.geometry.measure`` against Shapely as an independent reference.

Shapely is a test-only dependency: nothing in ``src/`` imports it. Closed shapes are compared as filled
polygons, so "distance 0" also covers one shape lying completely inside another; the boundary-to-boundary
gap is a different quantity and is checked separately. Circles become dense Shapely polygons, so circle
cases allow the polygon's sagitta as error instead of treating Shapely's answer as exact.
"""
from __future__ import annotations

import math
import random

import pytest
from shapely.geometry import LineString, Point, Polygon

from core.geometry.measure import entity_distance, zone_relation
from core.models.entities import CIRCLE_SEGMENTS
from core.rules.engine import compare

QUAD_SEGS = 1024                     # Shapely circle: 4 * QUAD_SEGS inscribed edges
TOL = 1e-6


def _sagitta(r: float, edges: int) -> float:
    return r * (1 - math.cos(math.pi / edges))


def to_shapely(e):
    """Filled region for closed shapes, the polyline itself for open ones."""
    if e.kind == "circle":
        return Point(e.geometry["center"]).buffer(e.geometry["radius"], quad_segs=QUAD_SEGS)
    pts = e.vertices()
    if len(set(pts)) == 1:
        return Point(pts[0])
    if e.closed:
        return Polygon(pts)
    return LineString(pts)


def boundary(e):
    g = to_shapely(e)
    return g.exterior if isinstance(g, Polygon) else g


def _star(rng, cx, cy, radius, n):
    """Simple (non self-intersecting) polygon, concave in general."""
    angles = sorted(rng.uniform(0, 2 * math.pi) for _ in range(n))
    return [(cx + radius * rng.uniform(0.35, 1.0) * math.cos(a),
             cy + radius * rng.uniform(0.35, 1.0) * math.sin(a)) for a in angles]


def _random_entity(ef, rng, layer):
    cx, cy = rng.uniform(-500, 500), rng.uniform(-500, 500)
    if rng.random() < 0.5:
        return ef.line(layer, *_star(rng, cx, cy, rng.uniform(20, 300), rng.randint(3, 9)), closed=True)
    return ef.line(layer, *[(cx + rng.uniform(-300, 300), cy + rng.uniform(-300, 300))
                            for _ in range(rng.randint(2, 4))])


# -- polylines and polygons ---------------------------------------------------------------------------------

def test_distance_matches_shapely_for_random_polylines_and_polygons(ef):
    rng = random.Random(2026)
    for _ in range(600):
        a, b = _random_entity(ef, rng, "A"), _random_entity(ef, rng, "B")
        expected = to_shapely(a).distance(to_shapely(b))
        r = entity_distance(a, b)
        assert r.distance == pytest.approx(expected, abs=TOL), (a.geometry, b.geometry)
        assert entity_distance(b, a).distance == pytest.approx(expected, abs=TOL)
        assert math.dist(r.point_a, r.point_b) == pytest.approx(r.distance, abs=TOL)
        if r.contained:
            assert boundary(a).distance(boundary(b)) > 0.0


@pytest.mark.parametrize("name, a_pts, a_closed, b_pts, b_closed", [
    ("parallel", [(0, 0), (1000, 0)], False, [(0, 300), (1000, 300)], False),
    ("parallel, offset ends", [(0, 0), (1000, 0)], False, [(1200, 300), (2000, 300)], False),
    ("touching at a vertex", [(0, 0), (100, 0)], False, [(100, 0), (100, 100)], False),
    ("touching mid-edge", [(0, 0), (100, 0)], False, [(50, 0), (50, 100)], False),
    ("crossing", [(0, 0), (100, 100)], False, [(0, 100), (100, 0)], False),
    ("collinear overlap", [(0, 0), (100, 0)], False, [(40, 0), (200, 0)], False),
    ("line in the notch of a concave polygon",
     [(0, 0), (300, 0), (300, 300), (200, 300), (200, 100), (100, 100), (100, 300), (0, 300)], True,
     [(150, 150), (150, 250)], False),
    ("line crossing the notch",
     [(0, 0), (300, 0), (300, 300), (200, 300), (200, 100), (100, 100), (100, 300), (0, 300)], True,
     [(50, 200), (250, 200)], False),
    ("zero-length line outside", [(0, 0), (100, 0), (100, 100), (0, 100)], True, [(150, 50), (150, 50)], False),
    ("zero-length line inside", [(0, 0), (100, 0), (100, 100), (0, 100)], True, [(50, 50), (50, 50)], False),
    ("polygon touching polygon", [(0, 0), (100, 0), (100, 100), (0, 100)], True,
     [(100, 0), (200, 0), (200, 100), (100, 100)], True),
])
def test_distance_matches_shapely_for_edge_cases(ef, name, a_pts, a_closed, b_pts, b_closed):
    a = ef.line("A", *a_pts, closed=a_closed)
    b = ef.line("B", *b_pts, closed=b_closed)
    expected = to_shapely(a).distance(to_shapely(b))
    assert entity_distance(a, b).distance == pytest.approx(expected, abs=TOL)
    assert entity_distance(b, a).distance == pytest.approx(expected, abs=TOL)


def test_full_containment_is_distance_zero_not_the_boundary_gap(ef):
    """A filled zone contains the rectangle: the measured distance is 0 and flagged as containment,
    while the gap between the two outlines is 100. Tests must not read the 0 as touching outlines."""
    big = ef.rect("ZONE", 0, 0, 1000, 1000)
    small = ef.rect("X", 100, 400, 600, 600)
    r = entity_distance(small, big)
    assert r.distance == 0.0 and r.contained
    assert to_shapely(big).contains(to_shapely(small))
    assert boundary(big).distance(boundary(small)) == pytest.approx(100.0)

    line = ef.line("X", (100, 500), (900, 500))
    r = entity_distance(line, big)
    assert r.distance == 0.0 and r.contained
    assert boundary(big).distance(to_shapely(line)) == pytest.approx(100.0)


def test_open_outline_does_not_contain(ef):
    """An open polyline tracing a square is a line, not a region: the point inside it is 40 away."""
    outline = ef.line("A", (0, 0), (100, 0), (100, 100), (0, 100), (0, 0))
    inner = ef.line("B", (40, 50), (60, 50))
    r = entity_distance(outline, inner)
    assert not r.contained
    assert r.distance == pytest.approx(to_shapely(outline).distance(to_shapely(inner)), abs=TOL)
    assert r.distance == pytest.approx(40.0)


@pytest.mark.parametrize("gap, op, limit, ok", [
    (300.0, ">=", 300.0, True),
    (300.0 - 0.5e-6, ">=", 300.0, True),       # inside the 1e-6 tolerance
    (300.0 - 2e-6, ">=", 300.0, False),
    (300.0, ">", 300.0, False),
    (300.0, "<=", 300.0, True),
    (300.0 + 2e-6, "<=", 300.0, False),
])
def test_threshold_boundary_on_measured_gap(ef, gap, op, limit, ok):
    a = ef.line("A", (0, 0), (1000, 0))
    b = ef.line("B", (0, gap), (1000, gap))
    measured = entity_distance(a, b).distance
    assert measured == pytest.approx(to_shapely(a).distance(to_shapely(b)), abs=1e-9)
    assert compare(measured, op, limit) is ok


# -- circles ------------------------------------------------------------------------------------------------

def test_circle_distance_matches_shapely_within_polygon_error(ef):
    rng = random.Random(7)
    for _ in range(400):
        c = (rng.uniform(-300, 300), rng.uniform(-300, 300))
        radius = rng.uniform(5, 400)
        circle = ef.circle("C", c, radius)
        other = (ef.circle("D", (rng.uniform(-300, 300), rng.uniform(-300, 300)), rng.uniform(5, 400))
                 if rng.random() < 0.25 else _random_entity(ef, rng, "B"))
        expected = to_shapely(circle).distance(to_shapely(other))
        err = _sagitta(radius, 4 * QUAD_SEGS) + _sagitta(radius, CIRCLE_SEGMENTS) + TOL
        if other.kind == "circle":
            err += _sagitta(other.geometry["radius"], 4 * QUAD_SEGS) + _sagitta(other.geometry["radius"],
                                                                                CIRCLE_SEGMENTS)
        assert entity_distance(circle, other).distance == pytest.approx(expected, abs=err)
        assert entity_distance(other, circle).distance == pytest.approx(expected, abs=err)


def test_circle_exact_cases(ef):
    circle = ef.circle("C", (0, 0), 100)
    tangent = ef.line("L", (-200, 100), (200, 100))
    assert entity_distance(circle, tangent).distance == pytest.approx(0.0, abs=1e-9)
    outside = ef.line("L", (-200, 400), (200, 400))
    assert entity_distance(circle, outside).distance == pytest.approx(300.0, abs=1e-9)
    inside = entity_distance(circle, ef.line("L", (-10, 0), (10, 0)))
    assert inside.distance == 0.0 and inside.contained


def test_circle_containment_includes_the_sliver_between_chord_and_arc(ef):
    """Containment uses the true disc, not the 180-gon: the sliver between a chord and the arc
    (up to r * (1 - cos 1deg), 0.15 for r = 1000) is inside the circle."""
    circle = ef.circle("ZONE", (0, 0), 1000)
    a = math.radians(1)                          # midway between 180-gon vertices at 0 and 2 degrees
    line = ef.line("S", (999.99 * math.cos(a), 999.99 * math.sin(a)), (999.95 * math.cos(a), 999.95 * math.sin(a)))
    assert to_shapely(circle).contains(to_shapely(line))
    r = entity_distance(circle, line)
    assert r.distance == 0.0 and r.contained
    assert zone_relation(line, circle) == "inside"


# -- zone relation ------------------------------------------------------------------------------------------

def _reference_relation(subject, zone) -> str:
    s, z = to_shapely(subject), to_shapely(zone)
    if z.covers(s):
        return "inside"
    if s.disjoint(z):
        return "outside"
    return "partial"


def test_zone_relation_matches_shapely_for_random_polygon_zones(ef):
    rng = random.Random(11)
    seen = set()
    for _ in range(600):
        zone = ef.line("ZONE", *_star(rng, rng.uniform(-200, 200), rng.uniform(-200, 200),
                                      rng.uniform(100, 500), rng.randint(3, 10)), closed=True)
        subject = _random_entity(ef, rng, "S")
        expected = _reference_relation(subject, zone)
        seen.add(expected)
        assert zone_relation(subject, zone) == expected, (subject.geometry, zone.geometry)
    assert seen == {"inside", "outside", "partial"}


def test_zone_relation_matches_shapely_for_random_circle_zones(ef):
    rng = random.Random(13)
    seen = set()
    for _ in range(600):
        zone = ef.circle("ZONE", (rng.uniform(-200, 200), rng.uniform(-200, 200)), rng.uniform(100, 500))
        subject = (ef.circle("S", (rng.uniform(-500, 500), rng.uniform(-500, 500)), rng.uniform(5, 200))
                   if rng.random() < 0.25 else _random_entity(ef, rng, "S"))
        expected = _reference_relation(subject, zone)
        seen.add(expected)
        assert zone_relation(subject, zone) == expected, (subject.geometry, zone.geometry)
    assert seen == {"inside", "outside", "partial"}


@pytest.mark.parametrize("subject_kind, expected", [
    ("line on the rim", "inside"),
    ("circle tangent inside", "inside"),
    ("circle tangent outside", "partial"),
    ("line tangent outside", "partial"),
])
def test_circle_zone_rim_cases(ef, subject_kind, expected):
    zone = ef.circle("ZONE", (0, 0), 100)
    subject = {
        "line on the rim": lambda: ef.line("S", (100, 0), (0, 100)),
        "circle tangent inside": lambda: ef.circle("S", (60, 0), 40),
        "circle tangent outside": lambda: ef.circle("S", (140, 0), 40),
        "line tangent outside": lambda: ef.line("S", (100, -50), (100, 50)),
    }[subject_kind]()
    assert zone_relation(subject, zone) == expected


@pytest.mark.parametrize("pts, closed, expected", [
    ([(10, 10), (90, 90)], False, "inside"),
    ([(0, 50), (100, 50)], False, "inside"),                           # endpoints on the zone outline
    ([(0, 0), (100, 0)], False, "inside"),                             # runs along the outline
    ([(150, 250), (150, 120)], False, "partial"),                      # leaves through the notch
    ([(120, 220), (180, 220)], False, "outside"),                      # sits in the notch
    ([(300, 300), (400, 400)], False, "partial"),                      # touches a corner from outside
    ([(-50, -50), (350, -50), (350, 350), (-50, 350)], True, "partial"),  # zone inside the subject
])
def test_zone_relation_matches_shapely_for_edge_cases(ef, pts, closed, expected):
    zone = ef.line("ZONE", (0, 0), (300, 0), (300, 300), (200, 300), (200, 200), (100, 200),
                   (100, 300), (0, 300), closed=True)
    subject = ef.line("S", *pts, closed=closed)
    assert _reference_relation(subject, zone) == expected
    assert zone_relation(subject, zone) == expected
