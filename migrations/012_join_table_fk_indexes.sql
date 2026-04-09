-- 012_join_table_fk_indexes.sql
-- Add indexes on the FK side of join tables.
-- PKs already cover user_id (leading column); these cover the reverse
-- direction for role/dept/vendor lookups and cascade performance.

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_user_roles_role
    ON user_roles (role_id);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_user_allowed_departments_dept
    ON user_allowed_departments (department_id);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_user_allowed_vendors_vendor
    ON user_allowed_vendors (vendor_id);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_user_denied_vendors_vendor
    ON user_denied_vendors (vendor_id);
