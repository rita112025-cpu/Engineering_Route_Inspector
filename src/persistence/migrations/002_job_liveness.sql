-- 002: worker liveness (crash recovery) and the spatial index used per run
ALTER TABLE runs ADD COLUMN worker_create_time REAL;
ALTER TABLE runs ADD COLUMN heartbeat_at TEXT;
ALTER TABLE runs ADD COLUMN index_kind TEXT NOT NULL DEFAULT 'grid';
CREATE INDEX ix_runs_drawing ON runs(project_id, drawing_id, status);
