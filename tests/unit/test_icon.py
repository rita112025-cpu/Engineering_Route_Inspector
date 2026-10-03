"""assets/eri.ico is drawn by scripts/make_icon.py: it must be a valid ICO and exactly what the script draws."""
from __future__ import annotations

import importlib.util
import struct
import zlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ICO = ROOT / "assets" / "eri.ico"


@pytest.fixture(scope="module")
def make_icon():
    spec = importlib.util.spec_from_file_location("make_icon", ROOT / "scripts" / "make_icon.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _entries(data: bytes):
    reserved, kind, count = struct.unpack("<HHH", data[:6])
    assert (reserved, kind) == (0, 1)
    for i in range(count):
        w, h, _, _, planes, bpp, size, offset = struct.unpack("<BBBBHHII", data[6 + 16 * i:22 + 16 * i])
        yield (w or 256, h or 256, planes, bpp, data[offset:offset + size])


def test_committed_icon_is_a_valid_ico_with_every_size():
    entries = list(_entries(ICO.read_bytes()))
    assert [e[0] for e in entries] == [16, 24, 32, 48, 64, 128, 256]
    for w, h, planes, bpp, blob in entries:
        assert (w, h, planes, bpp) == (w, w, 1, 32)
        assert blob[:8] == b"\x89PNG\r\n\x1a\n"
        assert struct.unpack(">II", blob[16:24]) == (w, h)
        # the PNG's pixel data really decodes to w*h RGBA pixels
        idat = b""
        pos = 8
        while pos < len(blob):
            length, kind = struct.unpack(">I4s", blob[pos:pos + 8])
            if kind == b"IDAT":
                idat += blob[pos + 8:pos + 8 + length]
            assert zlib.crc32(blob[pos + 4:pos + 8 + length]) & 0xFFFFFFFF == struct.unpack(">I", blob[pos + 8 + length:pos + 12 + length])[0]
            pos += 12 + length
        assert len(zlib.decompress(idat)) == h * (1 + w * 4)


def test_committed_icon_is_exactly_what_the_script_draws(make_icon):
    assert ICO.read_bytes() == make_icon.build_ico()


def test_the_picture_has_a_transparent_corner_the_route_and_the_issue_marker(make_icon):
    px = make_icon.render(64)
    at = lambda x, y: tuple(px[(y * 64 + x) * 4:(y * 64 + x) * 4 + 4])          # noqa: E731
    assert at(0, 0)[3] == 0                                      # outside the rounded square
    assert at(32, 60)[:3] == make_icon.NAVY and at(32, 60)[3] == 255
    assert at(int(0.5 * 64), int(0.52 * 64))[:3] == make_icon.ROUTE            # on the route
    assert at(int(0.79 * 64), int(0.27 * 64))[:3] == make_icon.ISSUE           # the issue marker
