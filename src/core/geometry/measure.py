"""Entity-level measurements built on primitives."""
from __future__ import annotations

import math
from dataclasses import dataclass

from core.models.entities import GeometryEntity, Point
from .primitives import (
    bbox_distance, closest_point_on_segment, collinear_overlap_length, line_intersection_point,
    point_in_polygon, segment_angle_deg, segment_distance, segments_intersect, off_axis_deg,
)


@dataclass
class DistanceResult:
    distance: float
    point_a: Point
    point_b: Point
    contained: bool = False  # one closed shape contains (part of) the other

    @property
    def midpoint(self) -> Point:
        return ((self.point_a[0] + self.point_b[0]) / 2, (self.point_a[1] + self.point_b[1]) / 2)


def _seg_bbox(s):
    (x0, y0), (x1, y1) = s
    return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


def _containment(a: GeometryEntity, b: GeometryEntity) -> Point | None:
    """If closed a contains a vertex of b (or vice versa) return that vertex."""
    for outer, inner in ((a, b), (b, a)):
        if not outer.closed:
            continue
        poly = outer.vertices()
        ob = outer.bbox
        for v in inner.vertices():
            if ob[0] <= v[0] <= ob[2] and ob[1] <= v[1] <= ob[3] and point_in_polygon(v, poly):
                return v
    return None


def _toward(c: Point, p: Point, r: float) -> Point:
    """Point on the circle (centre c, radius r) in the direction of p."""
    d = math.dist(c, p)
    if d <= 0.0:
        return (c[0] + r, c[1])
    return (c[0] + (p[0] - c[0]) / d * r, c[1] + (p[1] - c[1]) / d * r)


def _circle_segment(c: Point, r: float, a: Point, b: Point) -> tuple[float, Point, Point]:
    """Exact boundary distance between a circle and a segment: (distance, point on circle, point on segment)."""
    q = closest_point_on_segment(c, a, b)
    dmin = math.dist(c, q)
    da, db = math.dist(c, a), math.dist(c, b)
    dmax = max(da, db)
    if dmin >= r:                                   # segment entirely outside the circle
        return dmin - r, _toward(c, q, r), q
    if dmax <= r:                                   # segment entirely inside: distance to the rim
        far = a if da >= db else b
        return r - dmax, _toward(c, far, r), far
    # the segment crosses the rim: find where
    dx, dy = b[0] - a[0], b[1] - a[1]
    fx, fy = a[0] - c[0], a[1] - c[1]
    qa = dx * dx + dy * dy
    qb = 2 * (fx * dx + fy * dy)
    qc = fx * fx + fy * fy - r * r
    disc = max(0.0, qb * qb - 4 * qa * qc)
    for t in sorted(((-qb - math.sqrt(disc)) / (2 * qa), (-qb + math.sqrt(disc)) / (2 * qa))):
        if 0.0 <= t <= 1.0:
            p = (a[0] + t * dx, a[1] + t * dy)
            return 0.0, p, p
    return 0.0, q, q


def _exact_circle_distance(a: GeometryEntity, b: GeometryEntity) -> DistanceResult:
    """Boundary distance when at least one entity is a circle (no polygon approximation)."""
    if a.kind == "circle" and b.kind == "circle":
        ca, cb = tuple(a.geometry["center"]), tuple(b.geometry["center"])
        ra, rb = a.geometry["radius"], b.geometry["radius"]
        d = math.dist(ca, cb)
        if d >= ra + rb:
            return DistanceResult(d - ra - rb, _toward(ca, cb, ra), _toward(cb, ca, rb))
        if d + min(ra, rb) <= max(ra, rb):         # one inside the other
            gap = max(ra, rb) - d - min(ra, rb)
            return DistanceResult(gap, _toward(ca, cb, ra) if ra >= rb else ca,
                                  _toward(cb, ca, rb) if rb > ra else cb)
        mid = _toward(ca, cb, ra)
        return DistanceResult(0.0, mid, mid)
    circle, other, swapped = (a, b, False) if a.kind == "circle" else (b, a, True)
    c, r = tuple(circle.geometry["center"]), circle.geometry["radius"]
    best = DistanceResult(math.inf, circle.center(), other.center())
    for s0, s1 in other.segments():
        if bbox_distance(_seg_bbox((s0, s1)), circle.bbox) >= best.distance:
            continue
        d, pc, ps = _circle_segment(c, r, s0, s1)
        if d < best.distance:
            best = DistanceResult(d, ps, pc) if swapped else DistanceResult(d, pc, ps)
            if d == 0.0:
                break
    return best


def entity_distance(a: GeometryEntity, b: GeometryEntity, cutoff: float = math.inf) -> DistanceResult:
    """Minimum 2D distance between two entities (0 when touching/intersecting/contained).

    Circles are measured exactly; every other entity is a polyline whose
    vertices already approximate arcs/splines within the import tolerance.
    If the true distance exceeds ``cutoff`` a value > cutoff is returned
    (possibly the bbox lower bound) so callers can prune cheaply.
    """
    lb = bbox_distance(a.bbox, b.bbox)
    if lb > cutoff:
        ca, cb = a.center(), b.center()
        return DistanceResult(lb, ca, cb)
    if a.kind == "circle" or b.kind == "circle":
        best = _exact_circle_distance(a, b)
    else:
        best = DistanceResult(math.inf, a.center(), b.center())
        segs_b = [(s, _seg_bbox(s)) for s in b.segments()]
        done = False
        for sa in a.segments():
            bba = _seg_bbox(sa)
            for sb, bbb in segs_b:
                if bbox_distance(bba, bbb) >= best.distance:
                    continue
                d, pa, pb = segment_distance(sa[0], sa[1], sb[0], sb[1])
                if d < best.distance:
                    best = DistanceResult(d, pa, pb)
                    if d == 0.0:
                        done = True
                        break
            if done:
                break
    if best.distance > 0.0:
        v = _containment(a, b)
        if v is not None:
            return DistanceResult(0.0, v, v, contained=True)
    return best


def entities_intersect(a: GeometryEntity, b: GeometryEntity) -> list[Point]:
    """Boundary crossing/touching points (sampled, one per segment pair)."""
    if bbox_distance(a.bbox, b.bbox) > 0:
        return []
    pts: list[Point] = []
    segs_b = [(s, _seg_bbox(s)) for s in b.segments()]
    for sa in a.segments():
        bba = _seg_bbox(sa)
        for sb, bbb in segs_b:
            if bbox_distance(bba, bbb) > 0:
                continue
            if segments_intersect(sa[0], sa[1], sb[0], sb[1]):
                _, p, _ = segment_distance(sa[0], sa[1], sb[0], sb[1])
                pts.append(p)
    return pts


def overlap_length(a: GeometryEntity, b: GeometryEntity, tol: float = 1e-6) -> float:
    """Total length of collinear overlap between the boundaries of a and b."""
    if bbox_distance(a.bbox, b.bbox) > tol:
        return 0.0
    total = 0.0
    for sa in a.segments():
        for sb in b.segments():
            total += collinear_overlap_length(sa[0], sa[1], sb[0], sb[1], tol)
    return total


def _segment_inside_polygon(a: Point, b: Point, poly: list[Point]) -> bool:
    """True when the whole segment ab lies inside (or on the boundary of) the polygon.

    The segment is cut at every crossing with a polygon edge and each piece
    is tested at its midpoint, so a segment that leaves a concave polygon
    and re-enters it is detected even when its endpoints and midpoint are inside.
    """
    dx, dy = b[0] - a[0], b[1] - a[1]
    length2 = dx * dx + dy * dy
    if length2 <= 0.0:
        return point_in_polygon(a, poly)
    sx0, sx1 = min(a[0], b[0]), max(a[0], b[0])
    sy0, sy1 = min(a[1], b[1]), max(a[1], b[1])
    cuts = [0.0, 1.0]
    n = len(poly)
    for i in range(n):
        c, d = poly[i], poly[(i + 1) % n]
        if max(c[0], d[0]) < sx0 or min(c[0], d[0]) > sx1 or max(c[1], d[1]) < sy0 or min(c[1], d[1]) > sy1:
            continue
        if not segments_intersect(a, b, c, d):
            continue
        p = line_intersection_point(a, b, c, d)
        points = [p] if p is not None else [c, d]      # parallel: the overlap ends at c / d
        for q in points:
            t = ((q[0] - a[0]) * dx + (q[1] - a[1]) * dy) / length2
            if 0.0 < t < 1.0:
                cuts.append(t)
    cuts.sort()
    for t0, t1 in zip(cuts, cuts[1:]):
        if t1 - t0 <= 1e-12:
            continue
        tm = (t0 + t1) / 2
        if not point_in_polygon((a[0] + tm * dx, a[1] + tm * dy), poly):
            return False
    return True


def zone_relation(subject: GeometryEntity, zone: GeometryEntity) -> str:
    """'inside' (fully), 'outside' (no contact) or 'partial'."""
    if not zone.closed:
        return "outside"
    if bbox_distance(subject.bbox, zone.bbox) > 0:
        return "outside"
    poly = zone.vertices()
    inside_flags = [point_in_polygon(v, poly) for v in subject.vertices()]
    if all(inside_flags) and all(_segment_inside_polygon(a, b, poly) for a, b in subject.segments()):
        return "inside"
    if any(inside_flags) or entities_intersect(subject, zone):
        return "partial"
    # zone may lie completely within a closed subject
    if subject.closed and point_in_polygon(zone.vertices()[0], subject.vertices()):
        return "partial"
    return "outside"


def principal_angle(e: GeometryEntity) -> float | None:
    """Direction of the longest segment in [0, 180)."""
    segs = [s for s in e.segments() if s[0] != s[1]]
    if not segs or e.kind == "circle":
        return None
    a, b = max(segs, key=lambda s: math.dist(s[0], s[1]))
    return segment_angle_deg(a, b)


def max_off_axis(e: GeometryEntity) -> float | None:
    segs = [s for s in e.segments() if math.dist(s[0], s[1]) > 1e-9]
    if not segs or e.kind == "circle":
        return None
    return max(off_axis_deg(segment_angle_deg(a, b)) for a, b in segs)
