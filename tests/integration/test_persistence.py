import io
import sqlite3

import pytest

from core.analysis.pipeline import run_analysis
from core.models.results import EvidenceChunk
from core.rules.schema import normalize_ruleset
from persistence import db as DB
from persistence.repo import Repo
from persistence.storage import (
    Storage, StorageError, UploadTooLarge, check_id, resolve_inside, safe_filename,
)


@pytest.fixture
def conn(tmp_path):
    c = DB.open_database(tmp_path / "eri.sqlite3")
    yield c
    c.close()


# -- migrations ----------------------------------------------------------------

def test_fresh_database_gets_all_migrations(tmp_path):
    c = DB.connect(tmp_path / "x.sqlite3")
    applied = DB.migrate(c)
    assert applied == [m.version for m in DB.load_migrations()] and applied[:2] == [1, 2]
    assert DB.migrate(c) == []                          # idempotent
    assert DB.schema_version(c) == applied[-1]
    cols = {r["name"] for r in c.execute("PRAGMA table_info(runs)")}
    assert {"worker_create_time", "heartbeat_at", "index_kind"} <= cols
    assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def _write_migrations(d, files):
    d.mkdir()
    for name, sql in files.items():
        (d / name).write_text(sql, encoding="utf-8")
    return d


def test_failed_migration_rolls_back(tmp_path):
    mig = _write_migrations(tmp_path / "m", {
        "001_a.sql": "CREATE TABLE a (x INTEGER);",
        "002_b.sql": "CREATE TABLE b (x INTEGER);\nINSERT INTO nope VALUES (1);",
    })
    c = DB.connect(tmp_path / "x.sqlite3")
    with pytest.raises(DB.MigrationError, match="002_b"):
        DB.migrate(c, mig)
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "a" in tables and "b" not in tables         # 001 kept, 002 fully rolled back
    assert [r["version"] for r in c.execute("SELECT version FROM schema_migrations")] == [1]


def test_edited_or_newer_migrations_are_detected(tmp_path):
    mig = _write_migrations(tmp_path / "m", {"001_a.sql": "CREATE TABLE a (x INTEGER);"})
    c = DB.connect(tmp_path / "x.sqlite3")
    DB.migrate(c, mig)
    (mig / "001_a.sql").write_text("CREATE TABLE a (x TEXT);", encoding="utf-8")
    with pytest.raises(DB.MigrationError, match="checksum"):
        DB.migrate(c, mig)
    (mig / "001_a.sql").write_text("CREATE TABLE a (x INTEGER);", encoding="utf-8")
    c.execute("INSERT INTO schema_migrations VALUES (9, 'future', 'x', 'now')")
    with pytest.raises(DB.MigrationError, match="比目前程式新"):
        DB.migrate(c, mig)


def test_migration_file_naming_rules(tmp_path):
    with pytest.raises(DB.MigrationError, match="連續編號"):
        DB.load_migrations(_write_migrations(tmp_path / "gap", {"001_a.sql": "", "003_c.sql": ""}))
    with pytest.raises(DB.MigrationError, match="檔名格式"):
        DB.load_migrations(_write_migrations(tmp_path / "bad", {"1_a.sql": ""}))


def test_transaction_rolls_back_on_error(conn):
    repo = Repo(conn)
    p = repo.create_project("P")
    with pytest.raises(RuntimeError):
        with DB.transaction(conn):
            conn.execute("UPDATE projects SET name = 'changed' WHERE id = ?", (p["id"],))
            raise RuntimeError("boom")
    assert repo.get_project(p["id"])["name"] == "P"


# -- storage -------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("plan.dxf", "plan.dxf"),
    ("..\\..\\Windows\\system32\\evil.dxf", "evil.dxf"),
    ("../../etc/passwd", "passwd"),
    ("C:\\Users\\x\\圖面 A.dxf", "圖面 A.dxf"),
    ("a<b>c:d|e?.dxf", "a_b_c_d_e_.dxf"),
    ("CON.dxf", "_CON.dxf"),
    ("lpt1", "_lpt1"),
    ("...", "file"),
    ("", "file"),
    ("x\x00y.txt", "x_y.txt"),
])
def test_safe_filename(raw, expected):
    assert safe_filename(raw) == expected


def test_safe_filename_length_limit():
    name = safe_filename("a" * 500 + ".dxf")
    assert len(name) <= 120 and name.endswith(".dxf")


def test_resolve_inside_blocks_escape(tmp_path):
    assert resolve_inside(tmp_path, "a", "b.txt") == (tmp_path / "a" / "b.txt").resolve()
    for bad in ("..", "../x", "/etc/passwd"):
        with pytest.raises(StorageError):
            resolve_inside(tmp_path, bad)
    with pytest.raises(StorageError):
        check_id("../p_0000000000000000")
    assert check_id("p_0123456789abcdef") == "p_0123456789abcdef"


def test_save_stream_hashes_and_dedupes(tmp_path):
    st = Storage(tmp_path / "data")
    pid = "p_0123456789abcdef"
    st.project_dir(pid, create=True)
    a = st.save_stream(pid, "drawings", "../plan.dxf", io.BytesIO(b"hello"), max_bytes=100)
    b = st.save_stream(pid, "drawings", "plan.dxf", io.BytesIO(b"hello"), max_bytes=100)
    assert a.stored_name == b.stored_name and a.stored_name.endswith("_plan.dxf")
    assert a.path.parent == (tmp_path / "data/projects" / pid / "drawings").resolve()
    assert a.size == 5 and len(a.sha256) == 64
    with pytest.raises(UploadTooLarge):
        st.save_stream(pid, "drawings", "big.dxf", io.BytesIO(b"x" * 101), max_bytes=100)
    leftovers = list((tmp_path / "data/projects" / pid / "drawings").glob(".upload-*"))
    assert leftovers == []
    with pytest.raises(StorageError):
        st.file_path(pid, "drawings", "../../eri.sqlite3")
    with pytest.raises(StorageError):
        st.area(pid, "../other")


def test_cleanup_partial_uploads(tmp_path):
    st = Storage(tmp_path / "data")
    d = st.area("p_0123456789abcdef", "drawings")
    (d / ".upload-abc.part").write_bytes(b"x")
    assert st.cleanup_partial_uploads() == 1


# -- repository ----------------------------------------------------------------

RULESET = normalize_ruleset({
    "systems": [{"name": "SCADA", "layer_regex": "^SCADA"}, {"name": "POWER", "layer_regex": "^POWER"}],
    "rules": [{"id": "CLR", "name": "淨距", "subject": {"system": "SCADA"}, "target": {"system": "POWER"},
               "measurement": "horizontal_clearance", "operator": ">=", "value": 300,
               "evidence_ref": {"quote": "不得小於 300 mm"}}],
})


def test_project_drawing_document_rules_roundtrip(conn, ef):
    repo = Repo(conn)
    p = repo.create_project("示範")
    ents = [ef.line("SCADA", (0, 0), (100, 0)), ef.circle("POWER", (50, 50), 5)]
    d = repo.add_drawing(p["id"], "plan.dxf", "abc_plan.dxf", "f" * 64, 123, ents, unit_to_mm=1.0,
                         units_assumed=True, info={"layers": ["POWER", "SCADA"]})
    assert d["entity_count"] == 2 and d["units_assumed"] is True and d["info"]["layers"] == ["POWER", "SCADA"]
    back = repo.load_entities(d["id"])
    assert [e.to_dict() for e in back] == [dict(e.to_dict(), source_file="plan.dxf") for e in ents]
    assert repo.entity_rows(d["id"])[1]["g"]["kind"] == "circle"
    ev = EvidenceChunk("EV-1", "x", "spec.md", 1, 3, 4, "4.2", "不得小於 300 mm。", "h")
    doc = repo.add_document(p["id"], "spec.md", "abc_spec.md", "md", "e" * 64, 50, [ev], ["w"])
    assert doc["chunk_count"] == 1 and doc["warnings"] == ["w"]
    assert [c.text for c in repo.load_evidence(p["id"])] == ["不得小於 300 mm。"]
    assert repo.get_evidence(p["id"], "EV-1")["line_start"] == 3
    repo.save_ruleset(p["id"], RULESET)
    assert repo.get_ruleset(p["id"])["rules"] == RULESET["rules"]
    listed = repo.list_projects()[0]
    assert (listed["drawing_count"], listed["document_count"], listed["rule_count"]) == (1, 1, 1)
    ops = [a["operation"] for a in repo.recent_audit()]
    assert {"project.create", "drawing.import", "document.import", "rules.save"} <= set(ops)
    repo.delete_project(p["id"])
    assert conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0] == 0     # cascades
    assert conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 0


def test_run_lifecycle_and_issue_storage(conn, ef):
    repo = Repo(conn)
    p = repo.create_project("P")
    ents = [ef.line("SCADA", (0, 0), (5000, 0)), ef.line("POWER", (0, 250), (5000, 250)),
            ef.line("SCADA", (0, 9000), (5000, 9000)), ef.line("POWER", (0, 9500), (5000, 9500))]
    d = repo.add_drawing(p["id"], "plan.dxf", "s.dxf", "a" * 64, 1, ents, unit_to_mm=1.0, units_assumed=False,
                         info={})
    run = repo.create_run(p["id"], d, RULESET, None)
    assert run["status"] == "queued" and run["ruleset_sha256"] and run["software_version"]
    assert repo.claim_run(run["id"], 1234, 1.5)
    assert not repo.claim_run(run["id"], 1234, 1.5)                 # cannot claim twice
    repo.update_progress(run["id"], "rules", 0.5, "CLR")
    assert repo.get_run(run["id"])["stage"] == "rules"
    out = run_analysis(repo.load_entities(d["id"]), RULESET, drawing_key="plan.dxf")
    assert repo.complete_run(run["id"], out.results, {"counts": out.counts}, {})
    got = repo.get_run(run["id"])
    assert got["status"] == "completed" and got["progress"] == 1 and got["summary"]["counts"]["FAIL"] == 1
    issues, total = repo.list_issues(run["id"])
    assert total == 2 and issues[0]["status"] == "FAIL" and issues[0]["handles"] == sorted(
        [ents[0].handle, ents[1].handle])
    fails, n = repo.list_issues(run["id"], statuses=["FAIL"])
    assert n == 1 and fails[0]["measured"] == 250.0 and fails[0]["details"]["measurement_zh"] == "水平淨距"
    assert repo.list_issues(run["id"], search="250%_")[1] == 0       # LIKE wildcards escaped
    assert repo.get_issue(run["id"], fails[0]["issue_id"])["location"] == [0.0, 125.0]
    links = conn.execute("SELECT role, handle FROM issue_entities WHERE run_id = ? AND issue_id = ?",
                         (run["id"], fails[0]["issue_id"])).fetchall()
    assert sorted(tuple(r) for r in links) == [("subject", ents[0].handle), ("target", ents[1].handle)]
    assert repo.latest_completed_run(p["id"], "plan.dxf")["id"] == run["id"]
    assert repo.latest_completed_run(p["id"], "plan.dxf", exclude=run["id"]) is None


def test_cancel_queued_and_running(conn, ef):
    repo = Repo(conn)
    p = repo.create_project("P")
    d = repo.add_drawing(p["id"], "plan.dxf", "s.dxf", "a" * 64, 1, [ef.line("SCADA", (0, 0), (1, 0))],
                         unit_to_mm=1.0, units_assumed=False, info={})
    q = repo.create_run(p["id"], d, RULESET, None)
    assert repo.request_cancel(q["id"])["status"] == "cancelled"
    assert not repo.claim_run(q["id"], 1, None)
    r = repo.create_run(p["id"], d, RULESET, None)
    repo.claim_run(r["id"], 1, None)
    got = repo.request_cancel(r["id"])
    assert got["status"] == "running" and got["cancel_requested"] and repo.cancel_requested(r["id"])
    # a worker that finishes after a cancel request must not store results
    assert not repo.complete_run(r["id"], [], {}, {})
    assert repo.finish_run(r["id"], "cancelled", error_code="CANCELLED")
    assert not repo.finish_run(r["id"], "failed")                    # already final


def test_check_constraints_reject_bad_status(conn):
    repo = Repo(conn)
    p = repo.create_project("P")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO rules(project_id, rule_id, name, severity, rule_json, updated_at) "
                     "VALUES (?, 'R', 'R', 'INFO', '{}', 'now')", (p["id"],))
