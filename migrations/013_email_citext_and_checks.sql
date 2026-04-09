-- 013_email_citext_and_checks.sql
-- Switch users.email to citext so case-insensitive matching is handled by
-- the DB, not every Python call site.  Add a CHECK to reject whitespace
-- and empty strings at the storage layer.
--
-- user_context depends on users.email and must be recreated after the type change.

DROP VIEW IF EXISTS user_context;

CREATE EXTENSION IF NOT EXISTS citext;

ALTER TABLE users
    ALTER COLUMN email TYPE citext USING email::citext;

ALTER TABLE users
    ADD CONSTRAINT chk_users_email_trimmed
    CHECK (email = TRIM(email) AND email <> '');

CREATE OR REPLACE VIEW user_context AS
SELECT u.id, u.email, u.first_name, u.last_name,
       COALESCE(role_data.role_names, '{}'::text[]) AS role_names,
       COALESCE(dept_data.allowed_departments, '{}'::text[]) AS allowed_departments,
       COALESCE(av_data.allowed_vendor_ids, '{}'::text[]) AS allowed_vendor_ids,
       COALESCE(dv_data.denied_vendor_ids, '{}'::text[]) AS denied_vendor_ids,
       COALESCE(av_data.allowed_vendors, '[]'::jsonb) AS allowed_vendors
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
) dv_data ON true;
