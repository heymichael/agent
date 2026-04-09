-- 014_users_updated_at.sql
-- Add updated_at to users, matching the pattern vendors already has.

ALTER TABLE users
    ADD COLUMN updated_at TIMESTAMPTZ DEFAULT now();

CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_users_updated_at
    BEFORE UPDATE ON users
    FOR EACH ROW
    EXECUTE FUNCTION set_updated_at();
