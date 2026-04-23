-- 025_schema_migrations.sql
-- Create schema_migrations table to track applied migrations.
--
-- Task: 278 (CI-based database migration process)
--
-- This table is used by the CI migration runner to:
--   1. Track which migrations have been applied
--   2. Verify migration file integrity via SHA-256 checksum
--   3. Provide an audit trail with timestamps
--
-- After this migration runs, a bootstrap step populates the table with
-- migrations 001-024 (already applied manually before CI process existed).

CREATE TABLE IF NOT EXISTS schema_migrations (
    id SERIAL PRIMARY KEY,
    filename TEXT NOT NULL UNIQUE,
    checksum TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    applied_by TEXT
);

CREATE INDEX IF NOT EXISTS idx_schema_migrations_filename ON schema_migrations (filename);

COMMENT ON TABLE schema_migrations IS 'Tracks applied database migrations for idempotent CI execution';
COMMENT ON COLUMN schema_migrations.filename IS 'Migration filename (e.g., 025_schema_migrations.sql)';
COMMENT ON COLUMN schema_migrations.checksum IS 'SHA-256 hash of migration file contents at time of application';
COMMENT ON COLUMN schema_migrations.applied_at IS 'Timestamp when migration was applied';
COMMENT ON COLUMN schema_migrations.applied_by IS 'Service account or user that applied the migration (optional)';
