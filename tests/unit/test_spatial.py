import random

import pytest

from core.spatial.index import BruteForceIndex, GridIndex, build_index


def _random_boxes(rng, n, extent=10000.0, max_size=200.0):
    out = []
    for _ in range(n):
        x, y = rng.uniform(-extent, extent), rng.uniform(-extent, extent)
        w, h = rng.uniform(0, max_size), rng.uniform(0, max_size)
        out.append((x, y, x + w, y + h))
    return out


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_grid_matches_brute_force(seed):
    rng = random.Random(seed)
    boxes = _random_boxes(rng, 800)
    boxes += [(-20000, -5, 20000, 5)]            # very long item (goes to the "large" list)
    boxes += [(5.0, 5.0, 5.0, 5.0)] * 3           # degenerate points
    grid, brute = GridIndex(boxes), BruteForceIndex(boxes)
    for _ in range(300):
        q = _random_boxes(rng, 1, max_size=rng.choice([1.0, 500.0, 30000.0]))[0]
        assert grid.query(q) == brute.query(q)


def test_empty_and_identical_points():
    assert GridIndex([]).query((0, 0, 1, 1)) == []
    pts = [(7.0, 7.0, 7.0, 7.0)] * 10
    g = GridIndex(pts)
    assert g.query((6, 6, 8, 8)) == list(range(10))
    assert g.query((0, 0, 1, 1)) == []


def test_build_index_kinds():
    boxes = [(0, 0, 1, 1)]
    assert build_index(boxes, "brute").name == "brute-force"
    assert build_index(boxes).name == "grid"
