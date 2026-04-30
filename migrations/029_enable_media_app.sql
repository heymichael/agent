-- 029_enable_media_app.sql
-- Enable the media app for the haderach org (task 300).
--
-- The Digital Media MVP is deployed and ready. This migration adds 'media'
-- to the enabled_apps array for the haderach org so users can access it.
--
-- Idempotent: array_append is a no-op if the value is already present
-- when combined with the WHERE clause check.

UPDATE orgs
SET enabled_apps = array_append(enabled_apps, 'media')
WHERE slug = 'haderach'
  AND NOT ('media' = ANY(enabled_apps));

-- Rollback:
--   UPDATE orgs
--   SET enabled_apps = array_remove(enabled_apps, 'media')
--   WHERE slug = 'haderach';
