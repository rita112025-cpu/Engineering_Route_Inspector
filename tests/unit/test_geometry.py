import math
import random

import pytest

from core.geometry import primitives as G
from core.geometry.measure import (
    entity_distance, max_off_axis, overlap_length, principal_angle, zone_relation,
)


def test_closest_point_and_distance():
    assert G.closest_point_on_segment((5, 5), (0, 0), (10, 0)) == (5, 0)
    assert G.closest_point_on_segment((-3, 4), (0, 0), (10, 0)) == (0, 0)
    assert G.point_segment_distance((13, 4), (0, 0), (10, 0)) == pytest.approx(5.0)
    # degenerate segment
    assert G.point_segment_distance((3, 4), (0, 0), (0, 0)) == pytest.approx(5.0)


@pytest.mark.parametrize("a,b,c,d,expected", [
    ((0, 0), (10, 10), (0, 10), (10, 0), True),     # proper crossing
    ((0, 0), (10, 0), (10, 0), (20, 5), True),      # shared endpoint
    ((0, 0), (10, 0), (5, 0), (5, 5), True),        # T junction
    ((0, 0), (10, 0), (0, 1), (10, 1), False),      # parallel
    ((0, 0), (10, 0), (5, 0), (15, 0), True),       # collinear overlap
    ((0, 0), (10, 0), (11, 0), (15, 0), False),     # collinear, disjoint
    ((0, 0), (10, 0), (5, 0.001), (5, 5), False),   # near miss
])
def test_segments_intersect(a, b, c, d, expected):
    assert G.segments_intersect(a, b, c, d) is expected
    assert G.segments_intersect(c, d, a, b) is expected


def test_segment_distance_parallel_and_crossing():
    d, pa, pb = G.segment_distance((0, 0), (100, 0), (0, 250), (100, 250))
    assert d == pytest.approx(250.0)
    assert pa[1] == pytest.approx(0.0) and pb[1] == pytest.approx(250.0)
    d, pa, pb = G.segment_distance((0, 0), (10, 10), (0, 10), (10, 0))
    assert d == 0.0 and pa == pytest.approx((5.0, 5.0))


def test_segment_distance_matches_dense_sampling():
    """Property test: exact distance is never above a dense sample, and close to it."""
    rng = random.Random(1234)
    for _ in range(300):
        a, b, c, d = [(rng.uniform(-100, 100), rng.uniform(-100, 100)) for _ in range(4)]
        dist, _, _ = G.segment_distance(a, b, c, d)
        n = 200
        sample = min(
            G.point_segment_distance((c[0] + (d[0] - c[0]) * i / n, c[1] + (d[1] - c[1]) * i / n), a, b)
            for i in range(n + 1))
        seg_len = math.dist(c, d)
        assert dist <= sample + 1e-9
        assert dist >= sample - seg_len / n - 1e-9


def test_point_in_polygon_concave_and_boundary():
    # U shape (concave)
    u = [(0, 0), (30, 0), (30, 30), (20, 30), (20, 10), (10, 10), (10, 30), (0, 30)]
    assert G.point_in_polygon((5, 20), u)
    assert not G.point_in_polygon((15, 20), u)       # inside the notch
    assert G.point_in_polygon((15, 10), u)           # on boundary counts as inside
    assert not G.point_in_polygon((40, 5), u)
    assert not G.point_in_polygon((1, 1), [(0, 0), (1, 1)])  # not a polygon


def test_collinear_overlap_and_angles():
    assert G.collinear_overlap_length((0, 0), (10, 0), (5, 0), (20, 0)) == pytest.approx(5.0)
    assert G.collinear_overlap_length((0, 0), (10, 0), (5, 1), (20, 1)) == 0.0
    assert G.segment_angle_deg((0, 0), (1, 1)) == pytest.approx(45.0)
    assert G.segment_angle_deg((0, 0), (-1, 0)) == pytest.approx(0.0)
    assert G.off_axis_deg(93.0) == pytest.approx(3.0)
    assert G.off_axis_deg(45.0) == pytest.approx(45.0)
    assert G.bbox_distance((0, 0, 1, 1), (4, 5, 6, 6)) == pytest.approx(5.0)


def test_entity_distance_lines_and_circle(ef):
    a = ef.line("A", (0, 0), (1000, 0))
    b = ef.line("B", (0, 300), (1000, 300))
    assert entity_distance(a, b).distance == pytest.approx(300.0)
    c = ef.circle("C", (500, 600), 100)
    assert entity_distance(b, c).distance == pytest.approx(200.0, abs=0.01)


def test_entity_distance_containment(ef):
    big = ef.rect("ZONE", 0, 0, 1000, 1000)
    small = ef.rect("X", 400, 400, 600, 600)
    r = entity_distance(small, big)
    assert r.distance == 0.0 and r.contained


def test_entity_distance_cutoff_returns_value_above_cutoff(ef):
    a = ef.line("A", (0, 0), (10, 0))
    b = ef.line("B", (0, 5000), (10, 5000))
    r = entity_distance(a, b, cutoff=100.0)
    assert r.distance > 100.0


def test_zone_relation(ef):
    zone = ef.rect("Z", 0, 0, 100, 100)
    assert zone_relation(ef.line("S", (10, 10), (90, 90)), zone) == "inside"
    assert zone_relation(ef.line("S", (50, 50), (150, 50)), zone) == "partial"
    assert zone_relation(ef.line("S", (200, 0), (300, 0)), zone) == "outside"
    # zone completely inside a closed subject
    assert zone_relation(ef.rect("S", -50, -50, 150, 150), zone) == "partial"
    # open polyline cannot be a zone
    assert zone_relation(ef.line("S", (10, 10), (20, 20)), ef.line("Z", (0, 0), (100, 0))) == "outside"


def test_overlap_length(ef):
    a = ef.line("A", (0, 0), (100, 0))
    b = ef.line("B", (40, 0), (200, 0))
    assert overlap_length(a, b) == pytest.approx(60.0)
    assert overlap_length(a, ef.line("C", (0, 5), (100, 5))) == 0.0


def test_principal_and_off_axis(ef):
    e = ef.line("A", (0, 0), (100, 0), (100, 3))
    assert principal_angle(e) == pytest.approx(0.0)
    assert max_off_axis(e) == pytest.approx(0.0)
    tilted = ef.line("A", (0, 0), (100, 10))
    assert max_off_axis(tilted) == pytest.approx(math.degrees(math.atan2(10, 100)))
    assert principal_angle(ef.circle("C", (0, 0), 5)) is None
    assert max_off_axis(ef.text("T", (0, 0), "x")) is None


def _rim(c, r, n=4000):
    return [(c[0] + r * math.cos(2 * math.pi * i / n), c[1] + r * math.sin(2 * math.pi * i / n)) for i in range(n)]


def test_circle_segment_distance_matches_dense_sampling(ef):
    """Exact circle/polyline distance agrees with a brute-force sample of the rim (property test)."""
    rng = random.Random(99)
    for _ in range(250):
        c = (rng.uniform(-50, 50), rng.uniform(-50, 50))
        r = rng.uniform(1, 40)
        circle = ef.circle("C", c, r)
        pts = [(rng.uniform(-120, 120), rng.uniform(-120, 120)) for _ in range(rng.choice([2, 3]))]
        line = ef.line("L", *pts)
        res = entity_distance(circle, line)
        if res.contained:
            continue
        sample = min(G.point_segment_distance(q, a, b) for q in _rim(c, r)
                     for a, b in line.segments())
        assert res.distance <= sample + 1e-9
        assert res.distance >= sample - 2 * math.pi * r / 4000 - 1e-9
        # reported closest points realise the distance
        assert math.dist(res.point_a, res.point_b) == pytest.approx(res.distance, abs=1e-6)


def test_circle_circle_distance_cases(ef):
    a = ef.circle("A", (0, 0), 10)
    assert entity_distance(a, ef.circle("B", (30, 0), 5)).distance == pytest.approx(15.0)
    assert entity_distance(a, ef.circle("B", (12, 0), 5)).distance == 0.0           # overlapping rims
    inner = entity_distance(a, ef.circle("B", (1, 0), 3))
    assert inner.contained and inner.distance == 0.0
    assert entity_distance(ef.circle("B", (1, 0), 3), a).contained
