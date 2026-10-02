from __future__ import annotations

import os
from pathlib import Path

import ezdxf
import pytest

from importers.dxf import load_dxf
from jobs.analysis import execute_run
from tests.conftest import write_sample_dxf  # noqa: F401  (re-exported for the tests here)

from core.rules.schema import normalize_ruleset
from persistence.db import open_database
from persistence.repo import Repo
from persistence.storage import Storage, new_id

SYSTEMS = [{"name": "SCADA", "layer_regex": "^SCADA"}, {"name": "POWER", "layer_regex": "^POWER"}]
CLEARANCE = {"id": "CLR", "name": "SCADA/POWER 水平淨距", "subject": {"system": "SCADA"},
             "target": {"system": "POWER"}, "measurement": "horizontal_clearance", "operator": ">=",
             "value": 300, "warn_margin": 50}


def ruleset(*rules):
    return normalize_ruleset({"systems": SYSTEMS, "rules": list(rules) or [CLEARANCE]})


class Env:
    def __init__(self, tmp_path):
        self.storage = Storage(tmp_path / "data")
        self.conn = open_database(self.storage.db_path)
        self.repo = Repo(self.conn)

    def import_dxf(self, project, source: Path, logical_name: str | None = None):
        """Copy a real DXF into the project, parse it and store the drawing (like the upload endpoint)."""
        stored = self.storage.copy_in(project["id"], "drawings", source, 50 * 1024 * 1024)
        imp = load_dxf(stored.path, logical_name or source.name)
        return self.repo.add_drawing(
            project["id"], logical_name or source.name, stored.stored_name, stored.sha256, stored.size, imp.entities,
            unit_to_mm=imp.unit_to_mm, units_assumed=imp.units_assumed, info={"warnings": imp.warnings})

    def run_in_process(self, project, drawing, rules, baseline=None):
        run = self.repo.create_run(project["id"], drawing, rules, baseline)
        self.repo.claim_run(run["id"], os.getpid(), None)
        execute_run(self.repo, run["id"])
        return self.repo.get_run(run["id"])

    def project_with_drawing(self, entities, rules=None, logical_name="plan.dxf", project=None):
        p = project or self.repo.create_project("測試專案")
        self.storage.project_dir(p["id"], create=True)
        d = self.repo.add_drawing(p["id"], logical_name, f"{new_id('s')[2:]}_{logical_name}", "a" * 64, 1, entities,
                                  unit_to_mm=1.0, units_assumed=False, info={"warnings": []})
        rs = rules if rules is not None else ruleset()
        self.repo.save_ruleset(p["id"], rs)
        return p, d, rs


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.conn.close()

