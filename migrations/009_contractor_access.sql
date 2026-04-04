-- 009_contractor_access.sql
-- Adds contractor-level vendor permissioning:
--   1. is_contractor flag on vendors (default true = secure by default)
--   2. user_contractor_access join table for explicit per-user grants
--   3. Backfills existing vendors to is_contractor = false (preserves current access)

ALTER TABLE vendors ADD COLUMN is_contractor BOOLEAN NOT NULL DEFAULT true;

-- Existing vendors should remain accessible; new vendors default to locked.
UPDATE vendors SET is_contractor = false;

CREATE TABLE user_contractor_access (
    user_id    UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    vendor_id  UUID NOT NULL REFERENCES vendors(id) ON DELETE CASCADE,
    granted_by UUID NOT NULL REFERENCES users(id),
    granted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, vendor_id)
);

CREATE INDEX idx_contractor_access_vendor ON user_contractor_access(vendor_id);
