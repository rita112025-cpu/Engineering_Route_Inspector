"""DXF -> GeometryEntity. The only module (besides the DXF exporter) using ezdxf."""
from __future__ import annotations

import math
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from core.models.entities import GeometryEntity, make_circle, make_polyline, make_text

# $INSUNITS code -> millimetres per drawing unit (AutoCAD drawing-units table, codes 1-16 and 21-24;
# the astronomical units 17-20 are not supported and reported as such)
INSUNITS_TO_MM = {
    1: 25.4, 2: 304.8, 3: 1609344.0, 4: 1.0, 5: 10.0, 6: 1000.0, 7: 1_000_000.0,
    8: 0.0000254, 9: 0.0254, 10: 914.4, 11: 1e-7, 12: 1e-6, 13: 1e-3, 14: 100.0, 15: 10_000.0, 16: 100_000.0,
    21: 1200 / 3937 * 1000, 22: 25.4000508, 23: 3 * 1200 / 3937 * 1000, 24: 5280 * 1200 / 3937 * 1000,
}
INSUNITS_NAME = {0: "未指定", 1: "inch", 2: "feet", 3: "mile", 4: "mm", 5: "cm", 6: "m", 7: "km",
                 8: "microinch", 9: "mil", 10: "yard", 11: "angstrom", 12: "nm", 13: "µm", 14: "dm",
                 15: "dam", 16: "hm", 21: "US survey foot", 22: "US survey inch", 23: "US survey yard",
                 24: "US survey mile"}
CHORD_TOLERANCE_MM = 0.1   # arcs/ellipses/splines are polylines within this chord error
SUPPORTED = {"LINE", "LWPOLYLINE", "POLYLINE", "ARC", "CIRCLE", "INSERT", "TEXT", "MTEXT",
             "ELLIPSE", "SPLINE", "ATTRIB"}
MAX_BLOCK_DEPTH = 8
MAX_ENTITIES = 2_000_000
MAX_MINSERT = 100_000      # cells of one MINSERT array that are expanded


class DrawingImportError(Exception):
    def __init__(self, user_message: str, reason: str, detail: str = ""):
        super().__init__(f"{user_message}: {reason}")
        self.user_message = user_message
        self.reason = reason
        self.detail = detail


@dataclass
class DrawingImport:
    entities: list[GeometryEntity]
    unit_to_mm: float
    units_code: int
    units_assumed: bool
    layers: list[str]
    type_counts: dict[str, int]
    skipped: dict[str, int]
    warnings: list[str] = field(default_factory=list)
    parse_seconds: float = 0.0
    dxf_version: str = ""


def _open(path: Path):
    import ezdxf
    from ezdxf import recover
    try:
        return ezdxf.readfile(str(path)), []
    except IOError as exc:
        raise DrawingImportError("無法讀取 DXF", "檔案不存在、無法開啟，或不是 DXF 文字/二進位格式。", str(exc))
    except ezdxf.DXFStructureError:
        try:
            doc, auditor = recover.readfile(str(path))
        except Exception as exc:  # noqa: BLE001 - surface as user-level error
            raise DrawingImportError("無法讀取 DXF", "檔案格式錯誤或 DXF 版本不支援。", repr(exc))
        if auditor.has_errors:
            raise DrawingImportError("無法讀取 DXF", "檔案結構損毀，無法安全修復。",
                                     "; ".join(str(e.message) for e in auditor.errors[:5]))
        return doc, [f"DXF 結構有問題，已自動修復 {len(auditor.fixes)} 處"]
    except Exception as exc:  # noqa: BLE001
        raise DrawingImportError("無法讀取 DXF", "檔案格式錯誤或 DXF 版本不支援。", repr(exc))


class _Builder:
    def __init__(self, source_file: str, unit_to_mm: float):
        self.source = source_file
        self.sagitta = CHORD_TOLERANCE_MM / unit_to_mm if unit_to_mm else CHORD_TOLERANCE_MM
        self.capped = False
        self.entities: list[GeometryEntity] = []
        self.counts: Counter = Counter()
        self.skipped: Counter = Counter()
        self.warnings: list[str] = []

    def _meta(self, zs: list[float], extra: dict | None = None) -> dict:
        md = {}
        if zs:
            md["z_min"], md["z_max"] = min(zs), max(zs)
            md["z_known"] = any(abs(z) > 1e-9 for z in zs)
        else:
            md["z_known"] = False
        if extra:
            md.update(extra)
        return md

    def at_cap(self) -> bool:
        if len(self.entities) >= MAX_ENTITIES:
            if not self.capped:
                self.warnings.append(f"物件數超過上限 {MAX_ENTITIES}，其餘略過")
                self.capped = True
            return True
        return False

    def add(self, e, m, handle_path: str, layer: str, block_path: list[str]):
        from ezdxf import path as ezpath
        from ezdxf.math import Vec3
        if self.at_cap():
            return
        t = e.dxftype()
        hid = f"{self.source}#{handle_path}"
        extra = {"block_path": block_path} if block_path else {}
        try:
            if t in ("LINE", "LWPOLYLINE", "POLYLINE", "ARC", "ELLIPSE", "SPLINE"):
                if t == "POLYLINE" and (e.is_poly_face_mesh or e.is_polygon_mesh):
                    self.skipped["POLYLINE(mesh)"] += 1
                    return
                p = ezpath.make_path(e)
                if t == "ARC":      # vertices taken from the true circle (no Bezier approximation)
                    pts3 = list(e.flattening(self.sagitta))
                else:
                    pts3 = list(p.flattening(self.sagitta)) if len(p) or t == "LINE" else [p.start]
                if m is not None:
                    pts3 = list(m.transform_vertices(pts3))
                if len(pts3) < 2:
                    pts3 = pts3 * 2
                closed = bool(getattr(e, "closed", False)) or (t == "POLYLINE" and e.is_closed) or p.is_closed
                if t == "ELLIPSE" or t == "SPLINE":
                    closed = closed or (Vec3(pts3[0]).isclose(Vec3(pts3[-1])) and len(pts3) > 2)
                pts = [(v.x, v.y) for v in pts3]
                if closed and len(pts) > 2 and math.dist(pts[0], pts[-1]) < 1e-9:
                    pts = pts[:-1]
                extra2 = dict(extra)
                if t == "ARC":
                    extra2["source_kind"] = "ARC"
                self.entities.append(make_polyline(hid, handle_path, self.source, layer, t, pts, closed,
                                                   self._meta([v.z for v in pts3], extra2)))
            elif t == "CIRCLE":
                c = Vec3(e.dxf.center)
                r = float(e.dxf.radius)
                if m is None and e.dxf.extrusion.isclose((0, 0, 1)):
                    self.entities.append(make_circle(hid, handle_path, self.source, layer, (c.x, c.y), r,
                                                     self._meta([c.z], extra)))
                else:
                    pts3 = list(ezpath.make_path(e).flattening(self.sagitta))
                    if m is not None:
                        pts3 = list(m.transform_vertices(pts3))
                    ux = m.transform_direction(Vec3(1, 0, 0)) if m is not None else Vec3(1, 0, 0)
                    uy = m.transform_direction(Vec3(0, 1, 0)) if m is not None else Vec3(0, 1, 0)
                    uniform = abs(ux.magnitude - uy.magnitude) < 1e-9 and abs(ux.dot(uy)) < 1e-9
                    if uniform:
                        center = m.transform(e.dxf.center) if m is not None else c
                        self.entities.append(make_circle(hid, handle_path, self.source, layer,
                                                         (center.x, center.y), r * ux.magnitude,
                                                         self._meta([center.z], extra)))
                    else:
                        pts = [(v.x, v.y) for v in pts3]
                        if math.dist(pts[0], pts[-1]) < 1e-9:
                            pts = pts[:-1]
                        extra2 = dict(extra, source_kind="CIRCLE(non-uniform scaled)")
                        self.entities.append(make_polyline(hid, handle_path, self.source, layer, "CIRCLE", pts, True,
                                                           self._meta([v.z for v in pts3], extra2)))
            elif t in ("TEXT", "MTEXT", "ATTRIB"):
                ins = Vec3(e.dxf.insert)
                height = float(e.dxf.char_height if t == "MTEXT" else e.dxf.height)
                if m is not None:
                    ins = m.transform(ins)
                    height *= m.transform_direction(Vec3(0, 1, 0)).magnitude
                text = e.plain_text() if t in ("MTEXT", "TEXT") else e.dxf.text
                if t == "ATTRIB":
                    extra = dict(extra, tag=e.dxf.tag)
                self.entities.append(make_text(hid, handle_path, self.source, layer, t, (ins.x, ins.y),
                                               text, height, self._meta([ins.z], extra)))
            else:
                self.skipped[t] += 1
                return
            self.counts[t] += 1
        except Exception as exc:  # noqa: BLE001 - one bad entity must not kill the import
            self.skipped[t + "(error)"] += 1
            if len(self.warnings) < 20:
                self.warnings.append(f"物件 {handle_path}（{t}）無法解析：{exc}")

    def expand_insert(self, ins, doc, m, hp: str, layer: str, block_path: list[str], depth: int) -> None:
        name = ins.dxf.name
        if depth >= MAX_BLOCK_DEPTH:
            self.warnings.append(f"圖塊 {name}（{hp}）巢狀超過 {MAX_BLOCK_DEPTH} 層，停止展開")
            return
        if name in block_path:
            self.warnings.append(f"圖塊 {name}（{hp}）遞迴參照自己，停止展開")
            return
        block = doc.blocks.get(name)
        if block is None:
            self.warnings.append(f"找不到圖塊定義 {name}（{hp}）")
            return
        mi = ins.matrix44()
        mm = mi if m is None else mi * m  # apply block transform first, then parent
        self.walk(block, doc, mm, hp, layer, block_path + [name], depth + 1)

    def walk(self, entities, doc, m, prefix: str, parent_layer: str | None, block_path: list[str], depth: int):
        for e in entities:
            if self.at_cap():
                return
            t = e.dxftype()
            handle = e.dxf.handle or "?"
            hp = f"{prefix}>{handle}" if prefix else handle
            layer = e.dxf.get("layer", "0") if t in SUPPORTED else "0"   # unknown entity types have no layer attribute
            if parent_layer is not None and layer == "0":
                layer = parent_layer
            if t == "INSERT":
                self.counts["INSERT"] += 1
                cells = int(e.mcount)
                if cells > 1:                                   # MINSERT: rows x columns array
                    if cells > MAX_MINSERT:
                        self.warnings.append(
                            f"陣列插入 {e.dxf.name}（{hp}）有 {cells} 個單元，超過上限 {MAX_MINSERT}，未展開")
                        continue
                    for k, virtual in enumerate(e.multi_insert()):
                        self.expand_insert(virtual, doc, m, f"{hp}[{k}]", layer, block_path, depth)
                else:
                    self.expand_insert(e, doc, m, hp, layer, block_path, depth)
                for att in e.attribs:
                    self.add(att, m, f"{hp}>{att.dxf.handle}", att.dxf.layer, block_path + [e.dxf.name])
                continue
            if t not in SUPPORTED:
                self.skipped[t] += 1
                continue
            self.add(e, m, hp, layer, block_path)


def load_dxf(path: str | Path, source_name: str | None = None) -> DrawingImport:
    path = Path(path)
    t0 = time.perf_counter()
    doc, warnings = _open(path)
    code = int(doc.header.get("$INSUNITS", 0) or 0)
    unit_to_mm = INSUNITS_TO_MM.get(code)
    assumed = unit_to_mm is None
    if assumed:
        unit_to_mm = 1.0
        if code == 0:
            warnings.append("圖面未宣告單位（$INSUNITS），以 mm 推定")
        else:
            warnings.append(f"圖面單位代碼 $INSUNITS={code} 不支援，以 mm 推定")
    b = _Builder(source_name or path.name, unit_to_mm)
    b.warnings.extend(warnings)
    b.walk(doc.modelspace(), doc, None, "", None, [], 0)
    layers = sorted({e.layer for e in b.entities})
    return DrawingImport(
        entities=b.entities, unit_to_mm=unit_to_mm, units_code=code, units_assumed=assumed,
        layers=layers, type_counts=dict(b.counts), skipped=dict(b.skipped), warnings=b.warnings,
        parse_seconds=time.perf_counter() - t0, dxf_version=doc.dxfversion,
    )
