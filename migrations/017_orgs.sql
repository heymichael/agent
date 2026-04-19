-- 017_orgs.sql
-- Org tenancy lookup table. CMS is the source of truth for orgs; this is a
-- manually-maintained mirror in operational Postgres for slug-keyed scoping.
--
-- enabled_apps is TEXT[] (not a join table) per 197-r2. No FK to apps.slug
-- so Haderach can carry 'site' in enabled_apps before that app row exists
-- in the apps catalog.
--
-- Strategy: 197-r2. Task: 254. Deploy wave: 1.

CREATE TABLE orgs (
    slug         TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    enabled_apps TEXT[] NOT NULL DEFAULT '{}',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO orgs (slug, name, enabled_apps) VALUES
    ('arcade',   'Arcade',   '{expenses,vendors,vendor_administration,system_administration}'),
    ('haderach', 'Haderach', '{site,system_administration}');

-- Rollback:
--   DROP TABLE orgs;
