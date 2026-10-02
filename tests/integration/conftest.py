from __future__ import annotations

import pytest

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
