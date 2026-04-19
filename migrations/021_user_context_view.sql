-- 021_user_context_view.sql
-- Recreate user_context to expose the caller's org memberships as a jsonb
-- array of {slug, name, enabledApps}. No per-membership role field — global
-- roles in users.roles are the only role surface (197-r2 R2.6).
--
-- Other columns are unchanged from the definition in
-- migrations/013_email_citext_and_checks.sql; this migration only adds
-- `orgs`.
--
-- enabledApps is camelCased on purpose so /me can serve the column straight
-- through with no Python-side rename.
--
-- Strategy: 197-r2. Task: 254. Deploy wave: 1.

DROP VIEW IF EXISTS user_context;

CREATE VIEW user_context AS
SELECT u.id, u.email, u.first_name, u.last_name,
       COALESCE(role_data.role_names, '{}'::text[]) AS role_names,
       COALESCE(dept_data.allowed_departments, '{}'::text[]) AS allowed_departments,
       COALESCE(av_data.allowed_vendor_ids, '{}'::text[]) AS allowed_vendor_ids,
       COALESCE(dv_data.denied_vendor_ids, '{}'::text[]) AS denied_vendor_ids,
       COALESCE(av_data.allowed_vendors, '[]'::jsonb) AS allowed_vendors,
       COALESCE(org_data.orgs, '[]'::jsonb) AS orgs
FROM users u
LEFT JOIN LATERAL (
    SELECT array_agg(r.name ORDER BY r.name) AS role_names
    FROM user_roles ur
    JOIN roles r ON r.id = ur.role_id
    WHERE ur.user_id = u.id
) role_data ON true
LEFT JOIN LATERAL (
    SELECT array_agg(d.name ORDER BY d.name) AS allowed_departments
    FROM user_allowed_departments uad
    JOIN departments d ON d.id = uad.department_id
    WHERE uad.user_id = u.id
) dept_data ON true
LEFT JOIN LATERAL (
    SELECT array_agg(uav.vendor_id::text ORDER BY LOWER(COALESCE(v.name, uav.vendor_id::text))) AS allowed_vendor_ids,
           jsonb_agg(
               jsonb_build_object(
                   'id', uav.vendor_id::text,
                   'name', COALESCE(v.name, uav.vendor_id::text)
               )
               ORDER BY LOWER(COALESCE(v.name, uav.vendor_id::text))
           ) AS allowed_vendors
    FROM user_allowed_vendors uav
    LEFT JOIN vendors v ON v.id = uav.vendor_id
    WHERE uav.user_id = u.id
) av_data ON true
LEFT JOIN LATERAL (
    SELECT array_agg(udv.vendor_id::text ORDER BY udv.vendor_id::text) AS denied_vendor_ids
    FROM user_denied_vendors udv
    WHERE udv.user_id = u.id
) dv_data ON true
LEFT JOIN LATERAL (
    SELECT jsonb_agg(
               jsonb_build_object(
                   'slug',        o.slug,
                   'name',        o.name,
                   'enabledApps', o.enabled_apps
               )
               ORDER BY o.slug
           ) AS orgs
    FROM user_org_memberships uom
    JOIN orgs o ON o.slug = uom.org_slug
    WHERE uom.user_id = u.id
) org_data ON true;

-- Rollback:
--   DROP VIEW user_context;
--   -- then re-apply the CREATE OR REPLACE VIEW user_context block from
--   -- migrations/013_email_citext_and_checks.sql.
