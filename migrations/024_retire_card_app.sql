-- 024_retire_card_app.sql
-- Final cleanup for the card-app retirement (task 257).
--
-- Migration 023 already drained `user_roles` of any `haderach_user`
-- assignments. This migration finishes the job:
--
--   1. DELETE the `card` row from `apps`. The `app_granting_roles`
--      composite-FK row that links the card app to `haderach_user` is
--      removed automatically by the ON DELETE CASCADE on app_id.
--   2. DELETE the `haderach_user` row from `roles`. Safe now because:
--        - migration 023 emptied `user_roles` for that role
--        - step 1 above removed the only `app_granting_roles` row that
--          referenced it
--      The role had no other purpose — it existed solely to gate the
--      retired card app surface. See strategy 197-r2.
--
-- Idempotent: each DELETE is a no-op if the row is already gone.
--
-- Strategy: 197-r2. Task: 257. Coordinated with task 254 (multi-org
-- tenancy) per the 2026-04-20 decoupling decision.

DELETE FROM apps WHERE slug = 'card';

DELETE FROM roles WHERE name = 'haderach_user';

-- Rollback:
--   No structural rollback. To restore the card app and role for any
--   reason (e.g., re-staging a regression test), re-INSERT explicitly:
--
--     INSERT INTO roles (name) VALUES ('haderach_user');
--     INSERT INTO apps (slug, label, path, type, sort_order)
--       VALUES ('card', 'Card', '/card/', 'app', 2);
--     INSERT INTO app_granting_roles (app_id, role_id)
--       SELECT a.id, r.id
--       FROM apps a CROSS JOIN roles r
--       WHERE a.slug = 'card' AND r.name = 'haderach_user';
