"""Writes demo/demo_plan.dxf: a small cable-corridor plan used by the demo project.

The drawing is invented for demonstration. Units are millimetres. Each object was placed to produce one
particular result, which tests/regression/test_demo.py pins:

  SCADA-CABLE   S1..S7   weak-current cables (the objects being checked)
  POWER-CABLE   P1..P5   power cables
  WATER-PIPE    W1       a water pipe
  ZONE-NOGO     Z1       a closed no-go area (machine room)
  NOTE                   labels

Run:  python scripts/make_demo_dxf.py        (rewrites demo/demo_plan.dxf; the file only differs by timestamps)
"""
from __future__ import annotations

from pathlib import Path

import ezdxf

OUT = Path(__file__).resolve().parents[1] / "demo" / "demo_plan.dxf"


def build() -> ezdxf.document.Drawing:
    doc = ezdxf.new("R2018")
    doc.header["$INSUNITS"] = 4                      # millimetres: declared, so no "assumed units" warning
    for name, color in (("SCADA-CABLE", 5), ("POWER-CABLE", 1), ("WATER-PIPE", 4), ("ZONE-NOGO", 6), ("NOTE", 8)):
        doc.layers.add(name, color=color)
    msp = doc.modelspace()

    def line(layer, *pts, closed=False):
        return msp.add_lwpolyline(list(pts), close=closed, dxfattribs={"layer": layer}) if len(pts) > 2 or closed \
            else msp.add_line(pts[0], pts[1], dxfattribs={"layer": layer})

    def label(text, x, y, h=300):
        msp.add_text(text, height=h, dxfattribs={"layer": "NOTE", "insert": (x, y)})

    # three parallel corridors: 250 mm (too close), 320 mm (inside the warning band), 500 mm (fine)
    line("SCADA-CABLE", (0, 0), (24000, 0));        line("POWER-CABLE", (0, 250), (24000, 250))
    line("SCADA-CABLE", (0, 2000), (24000, 2000));  line("POWER-CABLE", (0, 2320), (24000, 2320))
    line("SCADA-CABLE", (0, 4200), (24000, 4200));  line("POWER-CABLE", (0, 4700), (24000, 4700))
    # S4: runs towards the machine room and into it
    line("SCADA-CABLE", (0, 6000), (12000, 6000), (12000, 9000))
    # S5: slightly off the horizontal and through the machine room
    line("SCADA-CABLE", (0, 10000), (15000, 10450))
    # machine room (no-go area)
    line("ZONE-NOGO", (11000, 7000), (16000, 7000), (16000, 10500), (11000, 10500), closed=True)
    # a water pipe crossing the middle corridor
    line("WATER-PIPE", (18000, 1000), (18000, 3000))
    # S6 crosses P4 in plan, no elevation in the drawing: the vertical clearance cannot be judged
    line("SCADA-CABLE", (6000, -3000), (6000, -1000));  line("POWER-CABLE", (4000, -2000), (9000, -2000))
    # S7 crosses P5 in plan, with elevations 1000 and 1100 mm: a 100 mm gap
    msp.add_line((20000, -3000, 1000), (20000, -1000, 1000), dxfattribs={"layer": "SCADA-CABLE"})
    msp.add_line((19000, -2000, 1100), (21000, -2000, 1100), dxfattribs={"layer": "POWER-CABLE"})

    label("SCADA-1", 500, 100);  label("POWER-1", 500, 350)
    label("SCADA-2", 500, 2100); label("POWER-2", 500, 2420)
    label("SCADA-3", 500, 4300); label("POWER-3", 500, 4800)
    label("機房（禁設區）", 11300, 10600)
    label("給水管", 18150, 3100)
    return doc


if __name__ == "__main__":
    OUT.parent.mkdir(exist_ok=True)
    build().saveas(OUT)
    print("wrote", OUT)
