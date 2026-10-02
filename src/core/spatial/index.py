"""Spatial indexes over entity bounding boxes.

``GridIndex`` is a uniform hash grid (pure Python, no native deps).
``BruteForceIndex`` has the same API and exists to verify the grid gives
identical candidate sets on small data.
"""
from __future__ import annotations

import math
from collections import defaultdict
from statistics import median
from typing import Iterable, Sequence

BBox = tuple[float, float, float, float]


def _overlaps(a: BBox, b: BBox) -> bool:
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


class BruteForceIndex:
    name = "brute-force"

    def __init__(self, bboxes: Sequence[BBox]):
        self.bboxes = list(bboxes)

    def query(self, box: BBox) -> list[int]:
        return [i for i, b in enumerate(self.bboxes) if _overlaps(b, box)]


class GridIndex:
    name = "grid"
    MAX_CELLS_PER_ITEM = 4096

    def __init__(self, bboxes: Sequence[BBox], cell_size: float | None = None):
        self.bboxes = list(bboxes)
        self.cell = cell_size or self._auto_cell(self.bboxes)
        self.grid: dict[tuple[int, int], list[int]] = defaultdict(list)
        self.large: list[int] = []  # items spanning too many cells; always checked
        for i, b in enumerate(self.bboxes):
            x0, y0, x1, y1 = self._cell_range(b)
            if (x1 - x0 + 1) * (y1 - y0 + 1) > self.MAX_CELLS_PER_ITEM:
                self.large.append(i)
                continue
            for cx in range(x0, x1 + 1):
                for cy in range(y0, y1 + 1):
                    self.grid[(cx, cy)].append(i)

    @staticmethod
    def _auto_cell(bboxes: Sequence[BBox]) -> float:
        if not bboxes:
            return 1.0
        sizes = [max(b[2] - b[0], b[3] - b[1]) for b in bboxes]
        med = median(sizes)
        minx = min(b[0] for b in bboxes); maxx = max(b[2] for b in bboxes)
        miny = min(b[1] for b in bboxes); maxy = max(b[3] for b in bboxes)
        extent = max(maxx - minx, maxy - miny, 1e-9)
        cell = med * 2 if med > 0 else extent / 64
        # keep the grid between ~1 and ~1e6 cells
        cell = max(cell, extent / 1000)
        return cell if cell > 0 else 1.0

    def _cell_range(self, b: BBox) -> tuple[int, int, int, int]:
        c = self.cell
        return (math.floor(b[0] / c), math.floor(b[1] / c), math.floor(b[2] / c), math.floor(b[3] / c))

    def query(self, box: BBox) -> list[int]:
        x0, y0, x1, y1 = self._cell_range(box)
        found: set[int] = set()
        ncells = (x1 - x0 + 1) * (y1 - y0 + 1)
        if ncells > len(self.grid):
            for cell_items in self.grid.values():
                found.update(cell_items)
        else:
            g = self.grid
            for cx in range(x0, x1 + 1):
                for cy in range(y0, y1 + 1):
                    items = g.get((cx, cy))
                    if items:
                        found.update(items)
        found.update(self.large)
        bbs = self.bboxes
        return sorted(i for i in found if _overlaps(bbs[i], box))


def build_index(bboxes: Iterable[BBox], kind: str = "grid"):
    bboxes = list(bboxes)
    if kind == "brute":
        return BruteForceIndex(bboxes)
    return GridIndex(bboxes)
