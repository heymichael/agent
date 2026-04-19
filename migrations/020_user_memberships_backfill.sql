-- 020_user_memberships_backfill.sql
-- Backfill org memberships for the 10 known users, then align role
-- assignments with the end-state in task 254's user table.
--
-- Approach:
--   1. Explicit memberships for the 10 known users (idempotent).
--   2. Defensive Arcade fallback for any users row not yet covered (catches
--      anyone added between snapshot pull and deploy).
--   3. Targeted role updates per the end-state column in task 254.
--   4. Defensive sweep that strips haderach_user from anyone who still has
--      it. Migration 023 drops the role itself once the card app is
--      retired (task 257).
--
-- Roles outside the migration's scope (`home`, `investor`) are preserved
-- everywhere they appear today.
--
-- Strategy: 197-r2. Task: 254. Deploy wave: 1.

-- 1. Explicit memberships per task 254's user table.
INSERT INTO user_org_memberships (user_id, org_slug)
SELECT u.id, m.org_slug
FROM (VALUES
    ('huy@heretic.fund'::citext,            'arcade'),
    ('mariam@heretic.fund'::citext,         'arcade'),
    ('mariam@heretic.ventures'::citext,     'arcade'),
    ('michael@heretic.fund'::citext,        'arcade'),
    ('suman@heretic.fund'::citext,          'arcade'),
    ('michael.d.mader@gmail.com'::citext,   'arcade'),
    ('michael@haderach.ai'::citext,         'haderach'),
    ('rene.saroukhanoff@gmail.com'::citext, 'haderach'),
    ('alexmader@gmail.com'::citext,         'haderach'),
    ('binamader@gmail.com'::citext,         'haderach')
) AS m(email, org_slug)
JOIN users u ON u.email = m.email
ON CONFLICT (user_id, org_slug) DO NOTHING;

-- 2. Defensive Arcade fallback for any uncovered users row.
INSERT INTO user_org_memberships (user_id, org_slug)
SELECT u.id, 'arcade'
FROM users u
WHERE NOT EXISTS (
    SELECT 1 FROM user_org_memberships uom WHERE uom.user_id = u.id
)
ON CONFLICT (user_id, org_slug) DO NOTHING;

-- 3a. Add `admin` to michael@haderach.ai (snapshot showed it was missing
--     despite seed_users.py implying otherwise).
INSERT INTO user_roles (user_id, role_id)
SELECT u.id, r.id
FROM users u
CROSS JOIN roles r
WHERE u.email = 'michael@haderach.ai'::citext
  AND r.name = 'admin'
ON CONFLICT DO NOTHING;

-- 3b. alexmader@gmail.com: drop haderach_user, add user.
--     home and investor are preserved (already present, untouched).
DELETE FROM user_roles
WHERE user_id = (SELECT id FROM users WHERE email = 'alexmader@gmail.com'::citext)
  AND role_id = (SELECT id FROM roles WHERE name = 'haderach_user');

INSERT INTO user_roles (user_id, role_id)
SELECT u.id, r.id
FROM users u
CROSS JOIN roles r
WHERE u.email = 'alexmader@gmail.com'::citext
  AND r.name = 'user'
ON CONFLICT DO NOTHING;

-- 3c. binamader@gmail.com: drop haderach_user, add user/home/investor.
DELETE FROM user_roles
WHERE user_id = (SELECT id FROM users WHERE email = 'binamader@gmail.com'::citext)
  AND role_id = (SELECT id FROM roles WHERE name = 'haderach_user');

INSERT INTO user_roles (user_id, role_id)
SELECT u.id, r.id
FROM users u
CROSS JOIN roles r
WHERE u.email = 'binamader@gmail.com'::citext
  AND r.name IN ('user', 'home', 'investor')
ON CONFLICT DO NOTHING;

-- 4. Defensive: strip any remaining haderach_user assignments. Migration
--    023 will drop the role itself in the cleanup wave.
DELETE FROM user_roles
WHERE role_id = (SELECT id FROM roles WHERE name = 'haderach_user');

-- Rollback:
--   This migration is not bidirectionally reversible without a snapshot of
--   the prior state because it both inserts memberships and rewrites role
--   assignments. To roll back, restore user_org_memberships and user_roles
--   from a pre-migration backup (or re-pull the prod snapshot if rolling
--   back locally). For a coarse local reset:
--     DELETE FROM user_org_memberships;
--   then re-apply the role state from the snapshot.
