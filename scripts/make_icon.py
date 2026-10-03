"""Draw assets/eri.ico: a cable route (orthogonal tray run) between two nodes, the far one marked as an issue.

    python scripts/make_icon.py            # writes assets/eri.ico
    python scripts/make_icon.py --check    # exit 1 when the committed file differs from what this script draws

Pure Python (zlib + struct, no imaging library). The picture is rasterised with 4x4 supersampling for every size
and stored as PNG entries inside the ICO container, so the output is identical on every run and every machine.
"""
from __future__ import annotations

import argparse
import math
import struct
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "assets" / "eri.ico"
SIZES = (16, 24, 32, 48, 64, 128, 256)

NAVY = (18, 53, 91)
ROUTE = (236, 244, 252)
ISSUE = (229, 72, 61)

# drawing, in a unit square (y grows downwards)
ROUTE_POINTS = [(0.21, 0.79), (0.21, 0.52), (0.50, 0.52), (0.50, 0.27), (0.79, 0.27)]
ROUTE_WIDTH = 0.085
START_NODE = ((0.21, 0.79), 0.115)
END_NODE = ((0.79, 0.27), 0.135)
END_NODE_INNER = 0.088
BG_MARGIN, BG_RADIUS = 0.035, 0.21
SS = 4                                                       # subsamples per axis


def _seg_dist(px: float, py: float, a: tuple[float, float], b: tuple[float, float]) -> float:
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _in_rounded_square(x: float, y: float) -> bool:
    lo, hi, r = BG_MARGIN, 1 - BG_MARGIN, BG_RADIUS
    if not (lo <= x <= hi and lo <= y <= hi):
        return False
    cx = min(max(x, lo + r), hi - r)
    cy = min(max(y, lo + r), hi - r)
    return math.hypot(x - cx, y - cy) <= r


def _sample(x: float, y: float) -> tuple[int, int, int, int] | None:
    """Colour of the picture at one point of the unit square, None when transparent."""
    if not _in_rounded_square(x, y):
        return None
    (ex, ey), er = END_NODE
    if math.hypot(x - ex, y - ey) <= END_NODE_INNER:
        return (*ISSUE, 255)
    if math.hypot(x - ex, y - ey) <= er:
        return (*ROUTE, 255)
    (sx, sy), sr = START_NODE
    if math.hypot(x - sx, y - sy) <= sr:
        return (*ROUTE, 255)
    for a, b in zip(ROUTE_POINTS, ROUTE_POINTS[1:]):
        if _seg_dist(x, y, a, b) <= ROUTE_WIDTH / 2:
            return (*ROUTE, 255)
    return (*NAVY, 255)


def render(size: int) -> bytes:
    """RGBA bytes (row by row) of the icon at ``size`` x ``size``."""
    rows = bytearray()
    for py in range(size):
        for px in range(size):
            acc = [0, 0, 0, 0]
            for sy in range(SS):
                for sx in range(SS):
                    c = _sample((px + (sx + 0.5) / SS) / size, (py + (sy + 0.5) / SS) / size)
                    if c is not None:
                        acc[0] += c[0]; acc[1] += c[1]; acc[2] += c[2]; acc[3] += 255
            n = SS * SS
            if acc[3] == 0:
                rows += b"\x00\x00\x00\x00"
            else:
                covered = acc[3] // 255
                rows += bytes((round(acc[0] / covered), round(acc[1] / covered), round(acc[2] / covered), round(acc[3] / n)))
    return bytes(rows)


def png(size: int, rgba: bytes) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + rgba[y * size * 4:(y + 1) * size * 4] for y in range(size))        # filter type 0 per row
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def build_ico(sizes=SIZES) -> bytes:
    images = [(s, png(s, render(s))) for s in sizes]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries, blobs = b"", b""
    for s, data in images:
        entries += struct.pack("<BBBBHHII", 0 if s >= 256 else s, 0 if s >= 256 else s, 0, 0, 1, 32, len(data), offset)
        blobs += data
        offset += len(data)
    return header + entries + blobs


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", action="store_true", help="only compare with the committed file")
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args(argv)
    data = build_ico()
    out = Path(args.out)
    if args.check:
        same = out.is_file() and out.read_bytes() == data
        print(f"{out}: {'up to date' if same else 'DIFFERS from what scripts/make_icon.py draws'}")
        return 0 if same else 1
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    print(f"wrote {out} ({len(data):,} bytes, sizes {', '.join(map(str, SIZES))})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
