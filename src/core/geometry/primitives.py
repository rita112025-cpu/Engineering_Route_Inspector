"""Pure 2D computational geometry. No CAD library dependencies."""
from __future__ import annotations

import math

Point = tuple[float, float]
EPS = 1e-9


def _orient(a: Point, b: Point, c: Point) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _on_segment(a: Point, b: Point, p: Point, tol: float) -> bool:
    return (min(a[0], b[0]) - tol <= p[0] <= max(a[0], b[0]) + tol
            and min(a[1], b[1]) - tol <= p[1] <= max(a[1], b[1]) + tol)


def _tol(*pts: Point) -> float:
    scale = max(1.0, *(max(abs(p[0]), abs(p[1])) for p in pts))
    return EPS * scale


def closest_point_on_segment(p: Point, a: Point, b: Point) -> Point:
    dx, dy = b[0] - a[0], b[1] - a[1]
    l2 = dx * dx + dy * dy
    if l2 <= 0.0:
        return a
    t = ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / l2
    t = 0.0 if t < 0.0 else 1.0 if t > 1.0 else t
    return (a[0] + t * dx, a[1] + t * dy)


def point_segment_distance(p: Point, a: Point, b: Point) -> float:
    return math.dist(p, closest_point_on_segment(p, a, b))


def segments_intersect(a: Point, b: Point, c: Point, d: Point) -> bool:
    """True when segments ab and cd share at least one point (touching counts)."""
    tol = _tol(a, b, c, d) * max(1.0, math.dist(a, b), math.dist(c, d))
    o1, o2 = _orient(a, b, c), _orient(a, b, d)
    o3, o4 = _orient(c, d, a), _orient(c, d, b)
    if ((o1 > tol and o2 < -tol) or (o1 < -tol and o2 > tol)) and \
       ((o3 > tol and o4 < -tol) or (o3 < -tol and o4 > tol)):
        return True
    t = _tol(a, b, c, d)
    if abs(o1) <= tol and _on_segment(a, b, c, t):
        return True
    if abs(o2) <= tol and _on_segment(a, b, d, t):
        return True
    if abs(o3) <= tol and _on_segment(c, d, a, t):
        return True
    if abs(o4) <= tol and _on_segment(c, d, b, t):
        return True
    return False


def line_intersection_point(a: Point, b: Point, c: Point, d: Point) -> Point | None:
    r = (b[0] - a[0], b[1] - a[1])
    s = (d[0] - c[0], d[1] - c[1])
    den = r[0] * s[1] - r[1] * s[0]
    if abs(den) < 1e-15:
        return None
    t = ((c[0] - a[0]) * s[1] - (c[1] - a[1]) * s[0]) / den
    return (a[0] + t * r[0], a[1] + t * r[1])


def segment_distance(a: Point, b: Point, c: Point, d: Point) -> tuple[float, Point, Point]:
    """Minimum distance between segments ab and cd plus the closest points."""
    if segments_intersect(a, b, c, d):
        p = line_intersection_point(a, b, c, d)
        if p is None:  # collinear overlap / degenerate: pick a shared point
            for q in (c, d):
                if point_segment_distance(q, a, b) <= _tol(a, b, c, d) * 10:
                    return 0.0, q, q
            for q in (a, b):
                if point_segment_distance(q, c, d) <= _tol(a, b, c, d) * 10:
                    return 0.0, q, q
            p = a
        return 0.0, p, p
    best = (math.inf, a, c)
    for p, s0, s1, first in ((a, c, d, True), (b, c, d, True), (c, a, b, False), (d, a, b, False)):
        q = closest_point_on_segment(p, s0, s1)
        dist = math.dist(p, q)
        if dist < best[0]:
            best = (dist, p, q) if first else (dist, q, p)
    return best


def point_in_polygon(p: Point, poly: list[Point]) -> bool:
    """Ray casting; points on the boundary count as inside."""
    n = len(poly)
    if n < 3:
        return False
    for i in range(n):
        a, b = poly[i], poly[(i + 1) % n]
        if point_segment_distance(p, a, b) <= _tol(p, a, b) * 10:
            return True
    x, y = p
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y):
            x_cross = (xj - xi) * (y - yi) / (yj - yi) + xi
            if x < x_cross:
                inside = not inside
        j = i
    return inside


def collinear_overlap_length(a: Point, b: Point, c: Point, d: Point, tol: float = 1e-6) -> float:
    """Length of the shared part of two collinear segments (0 if not collinear)."""
    la = math.dist(a, b)
    if la <= 0.0 or math.dist(c, d) <= 0.0:
        return 0.0
    if point_line_distance(c, a, b) > tol or point_line_distance(d, a, b) > tol:
        return 0.0
    ux, uy = (b[0] - a[0]) / la, (b[1] - a[1]) / la
    t0 = 0.0
    t1 = la
    s0 = (c[0] - a[0]) * ux + (c[1] - a[1]) * uy
    s1 = (d[0] - a[0]) * ux + (d[1] - a[1]) * uy
    lo, hi = max(t0, min(s0, s1)), min(t1, max(s0, s1))
    return max(0.0, hi - lo)


def point_line_distance(p: Point, a: Point, b: Point) -> float:
    la = math.dist(a, b)
    if la <= 0.0:
        return math.dist(p, a)
    return abs(_orient(a, b, p)) / la


def bbox_distance(a: tuple, b: tuple) -> float:
    dx = max(0.0, max(a[0], b[0]) - min(a[2], b[2]))
    dy = max(0.0, max(a[1], b[1]) - min(a[3], b[3]))
    return math.hypot(dx, dy)


def expand_bbox(b: tuple, r: float) -> tuple:
    return (b[0] - r, b[1] - r, b[2] + r, b[3] + r)


def segment_angle_deg(a: Point, b: Point) -> float:
    """Direction in [0, 180)."""
    ang = math.degrees(math.atan2(b[1] - a[1], b[0] - a[0])) % 180.0
    return 0.0 if abs(ang - 180.0) < 1e-9 else ang


def off_axis_deg(angle: float) -> float:
    """Deviation from the nearest multiple of 90 degrees, in [0, 45]."""
    m = angle % 90.0
    return min(m, 90.0 - m)
