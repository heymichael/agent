-- 018_user_org_memberships.sql
-- Multi-org-ready join table: users ↔ orgs.
--
-- Deliberately:
--   * No UNIQUE(user_id) constraint — schema supports multi-org membership
--     even though every user has exactly one membership today.
--   * No role column — global roles in users.roles via user_roles are the
--     only role surface (197-r2 R2.6).
--
-- Strategy: 197-r2. Task: 254. Deploy wave: 1.

CREATE TABLE user_org_memberships (
    user_id    UUID        NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    org_slug   TEXT        NOT NULL REFERENCES orgs(slug),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, org_slug)
);

-- Reverse-direction index for org_slug → user lookups; the PK already
-- covers user_id-leading queries.
CREATE INDEX idx_user_org_memberships_org_slug
    ON user_org_memberships (org_slug);

-- Rollback:
--   DROP TABLE user_org_memberships;
