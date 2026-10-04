-- 003: specification source, scanned files, rule candidates and the review decisions made on them
CREATE TABLE spec_settings (
    project_id      TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    source_path     TEXT,                                   -- the one folder the user chose to scan (NULL = app default)
    glossary_json   TEXT NOT NULL DEFAULT '{}',             -- project terms -> layer name patterns
    last_scan_json  TEXT,
    updated_at      TEXT NOT NULL
);

CREATE TABLE spec_files (
    project_id       TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    root_path        TEXT NOT NULL,
    rel_path         TEXT NOT NULL,                         -- relative to root_path, '/' separated
    size_bytes       INTEGER NOT NULL,
    mtime_ns         INTEGER NOT NULL,
    sha256           TEXT,
    kind             TEXT,
    status           TEXT NOT NULL,                         -- OK | NOT_EXTRACTABLE | UNREADABLE | TOO_LARGE
    reason           TEXT,
    document_id      TEXT REFERENCES documents(id) ON DELETE SET NULL,
    created_document INTEGER NOT NULL DEFAULT 0,            -- 1: the scan created the document (and may remove it again)
    scanned_at       TEXT NOT NULL,
    PRIMARY KEY (project_id, root_path, rel_path)
);
CREATE INDEX ix_spec_files_document ON spec_files(document_id);

CREATE TABLE doc_extraction (
    document_id        TEXT PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
    extractor_version  INTEGER NOT NULL,
    candidate_count    INTEGER NOT NULL,
    extracted_at       TEXT NOT NULL
);

CREATE TABLE rule_candidates (
    id            TEXT NOT NULL,                            -- stable: document sha256 + semantic key
    project_id    TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    document_id   TEXT NOT NULL,
    position      INTEGER NOT NULL,
    semantic_key  TEXT NOT NULL,
    evidence_id   TEXT NOT NULL,
    status        TEXT NOT NULL,                            -- last evaluated status (derived again on every read)
    raw_json      TEXT NOT NULL,                            -- the parser's output for the sentence
    created_at    TEXT NOT NULL,
    PRIMARY KEY (project_id, document_id, id),
    FOREIGN KEY (document_id, evidence_id) REFERENCES evidence(document_id, id) ON DELETE CASCADE
);
CREATE INDEX ix_rule_candidates_status ON rule_candidates(project_id, status);
CREATE INDEX ix_rule_candidates_id ON rule_candidates(project_id, id);

CREATE TABLE candidate_reviews (
    project_id     TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    review_key     TEXT NOT NULL,                           -- candidate id, or conflict group id
    semantic_key   TEXT,
    decision_json  TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    PRIMARY KEY (project_id, review_key)
);
