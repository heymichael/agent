-- 023_drop_haderach_user_role.sql
-- Defensive sweep: strip any remaining haderach_user assignments from
-- user_roles. The role was a legacy gate on the now-retired card app surface
-- (see strategy 197-r2 + task 257). Migration 020 step 4 already deleted
-- every assignment as part of the wave-1 backfill, so this migration is
-- expected to be a no-op against today's prod snapshot. Ship it anyway:
--
--   1. Safety net for any assignment that re-appears via seed_users.py
--      re-runs (the seed ran with haderach_user in the role list until
--      this Phase 9 cleanup; corresponding edit lands in the same PR).
--   2. Makes the invariant — "no user holds haderach_user" — explicit in
--      the migration history rather than buried inside step 4 of 020.
--
-- Naturally idempotent: a DELETE that matches zero rows is a no-op.
--
-- The role row itself is intentionally NOT dropped from the `roles` table.
-- agent/scripts/seed_apps.py still seeds the card app with a haderach_user
-- entry in app_granting_roles (the normalized join table that supersedes the
-- old apps.granting_roles column model); dropping the role row would break
-- seed runs. Task 257 (retire card app) owns the full cleanup: remove the
-- card app, drop the haderach_user grant from seed_apps.py, then a follow-up
-- migration can safely drop the role row from `roles`.
--
-- Strategy: 197-r2. Task: 254. Deploy wave: 2 (cleanup).

DELETE FROM user_roles
WHERE role_id = (SELECT id FROM roles WHERE name = 'haderach_user');

-- Rollback:
--   No structural rollback — this only deletes data that, by design, should
--   not exist. To restore a user to haderach_user (e.g., for a card-app
--   regression test), re-INSERT explicitly:
--     INSERT INTO user_roles (user_id, role_id)
--     SELECT u.id, r.id FROM users u CROSS JOIN roles r
--     WHERE u.email = 'someone@example.com' AND r.name = 'haderach_user';
