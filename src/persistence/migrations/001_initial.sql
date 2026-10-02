-- 001: initial schema
CREATE TABLE projects (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    root_path     TEXT NOT NULL,
    export_dir    TEXT,
    is_demo       INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE drawings (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    logical_name    TEXT NOT NULL,          -- original (sanitized) filename: stable identity
    stored_name     TEXT NOT NULL,          -- file name inside project/drawings/
    sha256          TEXT NOT NULL,
    size_bytes      INTEGER NOT NULL,
    imported_at     TEXT NOT NULL,
    entity_count    INTEGER,
    unit_to_mm      REAL,
    units_assumed   INTEGER,
    info_json       TEXT,
    UNIQUE (project_id, stored_name)
);
CREATE INDEX ix_drawings_project ON drawings(project_id);

CREATE TABLE documents (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    filename        TEXT NOT NULL,
    stored_name     TEXT NOT NULL,
    kind            TEXT NOT NULL,
    sha256          TEXT NOT NULL,
    size_bytes      INTEGER NOT NULL,
    imported_at     TEXT NOT NULL,
    chunk_count     INTEGER NOT NULL DEFAULT 0,
    warnings_json   TEXT,
    UNIQUE (project_id, stored_name)
);
CREATE INDEX ix_documents_project ON documents(project_id);

CREATE TABLE evidence (
    id              TEXT NOT NULL,
    document_id     TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    filename        TEXT NOT NULL,
    page            INTEGER,
    line_start      INTEGER NOT NULL,
    line_end        INTEGER NOT NULL,
    section         TEXT,
    text            TEXT NOT NULL,
    hash            TEXT NOT NULL,
    PRIMARY KEY (document_id, id)
);
CREATE INDEX ix_evidence_id ON evidence(id);

CREATE TABLE entities (
    drawing_id      TEXT NOT NULL REFERENCES drawings(id) ON DELETE CASCADE,
    seq             INTEGER NOT NULL,
    eid             TEXT NOT NULL,
    handle          TEXT NOT NULL,
    layer           TEXT NOT NULL,
    entity_type     TEXT NOT NULL,
    minx REAL, miny REAL, maxx REAL, maxy REAL,
    geometry_json   TEXT NOT NULL,
    metadata_json   TEXT,
    PRIMARY KEY (drawing_id, seq)
);
CREATE INDEX ix_entities_handle ON entities(drawing_id, handle);
CREATE INDEX ix_entities_layer ON entities(drawing_id, layer);

CREATE TABLE rules (
    project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    rule_id         TEXT NOT NULL,
    position        INTEGER NOT NULL DEFAULT 0,
    name            TEXT NOT NULL,
    severity        TEXT NOT NULL CHECK (severity IN ('FAIL','WARNING')),
    enabled         INTEGER NOT NULL DEFAULT 1,
    rule_json       TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (project_id, rule_id)
);

CREATE TABLE project_settings (
    project_id      TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    systems_json    TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE runs (
    id                TEXT PRIMARY KEY,
    project_id        TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    drawing_id        TEXT NOT NULL REFERENCES drawings(id) ON DELETE CASCADE,
    status            TEXT NOT NULL CHECK (status IN ('queued','running','completed','failed','cancelled')),
    stage             TEXT,
    stage_note        TEXT,
    progress          REAL NOT NULL DEFAULT 0,
    cancel_requested  INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL,
    started_at        TEXT,
    finished_at       TEXT,
    error_code        TEXT,
    error_message     TEXT,
    error_detail      TEXT,
    worker_pid        INTEGER,
    software_version  TEXT NOT NULL,
    drawing_sha256    TEXT,
    ruleset_json      TEXT,
    ruleset_sha256    TEXT,
    baseline_run_id   TEXT REFERENCES runs(id) ON DELETE SET NULL,
    summary_json      TEXT
);
CREATE INDEX ix_runs_project ON runs(project_id, created_at);
CREATE INDEX ix_runs_status ON runs(status);

CREATE TABLE issues (
    run_id          TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    issue_id        TEXT NOT NULL,
    seq             INTEGER NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('FAIL','WARNING','PASS','UNKNOWN')),
    severity        TEXT NOT NULL,
    confidence      TEXT NOT NULL CHECK (confidence IN ('CONFIRMED','INFERRED','UNKNOWN')),
    rule_id         TEXT NOT NULL,
    rule_name       TEXT NOT NULL,
    measurement     TEXT NOT NULL,
    layer           TEXT,
    system          TEXT,
    handle          TEXT,
    x REAL, y REAL,
    measured        REAL,
    required_op     TEXT,
    required_value  REAL,
    unit            TEXT,
    evidence_id     TEXT,
    message         TEXT NOT NULL,
    reason          TEXT,
    fix             TEXT,
    details_json    TEXT,
    baseline_state  TEXT,
    PRIMARY KEY (run_id, issue_id)
);
CREATE INDEX ix_issues_status ON issues(run_id, status);
CREATE INDEX ix_issues_rule ON issues(run_id, rule_id);
CREATE INDEX ix_issues_issue ON issues(issue_id);

CREATE TABLE issue_entities (
    run_id          TEXT NOT NULL,
    issue_id        TEXT NOT NULL,
    role            TEXT NOT NULL CHECK (role IN ('subject','target')),
    eid             TEXT NOT NULL,
    handle          TEXT NOT NULL,
    PRIMARY KEY (run_id, issue_id, role, eid),
    FOREIGN KEY (run_id, issue_id) REFERENCES issues(run_id, issue_id) ON DELETE CASCADE
);
CREATE INDEX ix_issue_entities_handle ON issue_entities(run_id, handle);

CREATE TABLE exports (
    id              TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    format          TEXT NOT NULL CHECK (format IN ('csv','markdown','html','dxf')),
    path            TEXT NOT NULL,
    sha256          TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE INDEX ix_exports_run ON exports(run_id);

CREATE TABLE audit_log (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    ts               TEXT NOT NULL,
    operation        TEXT NOT NULL,
    project_id       TEXT,
    input_json       TEXT,
    result           TEXT,
    software_version TEXT NOT NULL
);
CREATE INDEX ix_audit_ts ON audit_log(ts);
