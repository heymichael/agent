CREATE TABLE sync_job_log (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  job_name    TEXT NOT NULL,
  status      TEXT NOT NULL,
  started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at TIMESTAMPTZ,
  duration_ms INTEGER,
  error       TEXT,
  metadata    JSONB DEFAULT '{}'
);

CREATE INDEX idx_sync_job_log_name_started ON sync_job_log(job_name, started_at DESC);

CREATE TABLE sync_job_step (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  job_id      UUID NOT NULL REFERENCES sync_job_log(id),
  step_name   TEXT NOT NULL,
  status      TEXT NOT NULL,
  started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at TIMESTAMPTZ,
  duration_ms INTEGER,
  row_count   INTEGER,
  error       TEXT,
  metadata    JSONB DEFAULT '{}'
);

CREATE INDEX idx_sync_job_step_job ON sync_job_step(job_id);
