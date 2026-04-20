-- 022_drop_vendors_org_slug_default.sql
-- Drop the DEFAULT 'arcade' from vendors.org_slug. Migration 019 added it as
-- a deliberate bridge so the wave-1 (old) agent code, which didn't supply
-- org_slug on INSERT, kept writing during the wave-1 / wave-2 gap. By Phase 4
-- every writer (agent/service/cms_tools.py, agent/service/tools.py, the
-- vendors and expenses frontends, the CMS) sets org_slug explicitly via
-- _resolve_active_org_slug(). The DEFAULT is now a footgun: any future code
-- path that forgets to set org_slug would silently land in Arcade.
--
-- ALTER TABLE ... DROP DEFAULT is idempotent in Postgres — re-running this
-- migration on a column that already has no default is a no-op (no error).
--
-- The NOT NULL constraint stays. Existing rows are unaffected.
--
-- Strategy: 197-r2. Task: 254. Deploy wave: 2 (cleanup).

ALTER TABLE vendors
    ALTER COLUMN org_slug DROP DEFAULT;

-- Rollback:
--   ALTER TABLE vendors ALTER COLUMN org_slug SET DEFAULT 'arcade';
