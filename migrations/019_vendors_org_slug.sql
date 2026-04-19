-- 019_vendors_org_slug.sql
-- Add org_slug to vendors. The DEFAULT 'arcade' is a deliberate bridge so
-- the currently-running old agent code (which doesn't supply org_slug on
-- INSERT) keeps writing during the wave-1 / wave-2 gap. Existing rows are
-- backfilled by the DEFAULT itself.
--
-- This DEFAULT is correct, not arbitrary: the apps that write vendors during
-- the bridge — expenses, vendors, vendor_administration — all ship in
-- Arcade's enabled_apps; Haderach's enabled_apps is {site,
-- system_administration} and writes nothing to vendors. Every concurrent
-- writer IS an Arcade user. Migration 022 drops this DEFAULT once wave 2 is
-- live and new code supplies org_slug explicitly.
--
-- A regular CREATE INDEX (no CONCURRENTLY) is fine here — vendors is small
-- (~957 rows in prod) and the brief lock is acceptable.
--
-- Strategy: 197-r2. Task: 254. Deploy wave: 1.

ALTER TABLE vendors
    ADD COLUMN org_slug TEXT NOT NULL DEFAULT 'arcade'
        REFERENCES orgs(slug);

CREATE INDEX vendors_org_slug_idx ON vendors (org_slug);

-- Rollback:
--   DROP INDEX vendors_org_slug_idx;
--   ALTER TABLE vendors DROP COLUMN org_slug;
