"""Entity-level measurements built on primitives."""
from __future__ import annotations

import math
from dataclasses import dataclass

from core.models.entities import GeometryEntity, Point
from .primitives import (
    bbox_distance, collinear_overlap_length, point_in_polygon, segment_angle_deg,
    segment_distance, segments_intersect, off_axis_deg,
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


def entity_distance(a: GeometryEntity, b: GeometryEntity, cutoff: float = math.inf) -> DistanceResult:
    """Minimum 2D distance between two entities (0 when touching/intersecting/contained).

    If the true distance exceeds ``cutoff`` a value > cutoff is returned
    (possibly the bbox lower bound) so callers can prune cheaply.
    """
    lb = bbox_distance(a.bbox, b.bbox)
    if lb > cutoff:
        ca, cb = a.center(), b.center()
        return DistanceResult(lb, ca, cb)
    best = DistanceResult(math.inf, a.center(), b.center())
    segs_b = [(s, _seg_bbox(s)) for s in b.segments()]
    for sa in a.segments():
        bba = _seg_bbox(sa)
        for sb, bbb in segs_b:
            if bbox_distance(bba, bbb) >= best.distance:
                continue
            d, pa, pb = segment_distance(sa[0], sa[1], sb[0], sb[1])
            if d < best.distance:
                best = DistanceResult(d, pa, pb)
                if d == 0.0:
                    return best
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


def zone_relation(subject: GeometryEntity, zone: GeometryEntity) -> str:
    """'inside' (fully), 'outside' (no contact) or 'partial'."""
    if not zone.closed:
        return "outside"
    if bbox_distance(subject.bbox, zone.bbox) > 0:
        return "outside"
    poly = zone.vertices()
    inside_flags = [point_in_polygon(v, poly) for v in subject.vertices()]
    crosses = bool(entities_intersect(subject, zone))
    if all(inside_flags) and not _crosses_strictly(subject, zone):
        return "inside"
    if any(inside_flags) or crosses:
        return "partial"
    # zone may lie completely within a closed subject
    if subject.closed and point_in_polygon(zone.vertices()[0], subject.vertices()):
        return "partial"
    return "outside"


def _crosses_strictly(subject: GeometryEntity, zone: GeometryEntity) -> bool:
    """True when a subject segment leaves the zone (midpoints outside)."""
    poly = zone.vertices()
    for a, b in subject.segments():
        mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
        if not point_in_polygon(mid, poly):
            return True
    return False


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
