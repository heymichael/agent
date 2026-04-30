-- 030_add_media_app.sql
-- Add the media app to the apps table and grant access to user/admin roles.
--
-- Task: 300 - Digital Media MVP
--
-- The media app was added to the client-side APP_CATALOG (shared-ui) and
-- enabled for the haderach org (migration 029). This migration adds the
-- app definition to the database so it appears in the admin UI and is
-- properly validated by the backend.
--
-- Idempotent: uses ON CONFLICT DO NOTHING for all inserts.

INSERT INTO apps (slug, label, path, type, sort_order)
VALUES ('media', 'Media', '/media/', 'app', 40)
ON CONFLICT (slug) DO NOTHING;

INSERT INTO app_granting_roles (app_id, role_id)
SELECT a.id, r.id
FROM apps a CROSS JOIN roles r
WHERE a.slug = 'media' AND r.name = 'user'
ON CONFLICT DO NOTHING;

INSERT INTO app_granting_roles (app_id, role_id)
SELECT a.id, r.id
FROM apps a CROSS JOIN roles r
WHERE a.slug = 'media' AND r.name = 'admin'
ON CONFLICT DO NOTHING;

-- Rollback:
--   DELETE FROM app_granting_roles WHERE app_id = (SELECT id FROM apps WHERE slug = 'media');
--   DELETE FROM apps WHERE slug = 'media';
