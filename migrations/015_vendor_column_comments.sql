-- 015_vendor_column_comments.sql
-- Display view for vendor table columns, read by TableConfig.from_table()
-- at agent startup. The view promotes the inline _VENDOR_LIST_SQL JOINs to a
-- named DB object. Columns without a COMMENT are excluded as internal.

DROP VIEW IF EXISTS vendor_display_v;
CREATE VIEW vendor_display_v AS
SELECT v.name,
       v.source_system,
       v.source_system_id,
       v.payment_method,
       v.billing_frequency,
       v.account_type,
       v.track_1099,
       v.purpose,
       v.spend_type,
       v.contract_start,
       v.contract_end,
       v.contract_months,
       v.auto_renew,
       v.renewal_rate,
       v.renewal_notice,
       v.termination_terms,
       v.created_at,
       v.modified_at,
       v.synced_at,
       d.name  AS department,
       uo.email::text AS owner,
       us.email::text AS secondary_owner
FROM vendors v
LEFT JOIN departments d  ON d.id = v.department_id
LEFT JOIN users uo       ON uo.id = v.owner_id
LEFT JOIN users us       ON us.id = v.secondary_owner_id;

COMMENT ON COLUMN vendor_display_v.name IS 'Vendor';
COMMENT ON COLUMN vendor_display_v.source_system IS 'Source system';
COMMENT ON COLUMN vendor_display_v.source_system_id IS 'Source system ID';
COMMENT ON COLUMN vendor_display_v.payment_method IS 'Payment method';
COMMENT ON COLUMN vendor_display_v.billing_frequency IS 'Billing frequency';
COMMENT ON COLUMN vendor_display_v.account_type IS 'Account type';
COMMENT ON COLUMN vendor_display_v.track_1099 IS '1099 tracked';
COMMENT ON COLUMN vendor_display_v.purpose IS 'Purpose';
COMMENT ON COLUMN vendor_display_v.spend_type IS 'Spend type';
COMMENT ON COLUMN vendor_display_v.contract_start IS 'Contract start';
COMMENT ON COLUMN vendor_display_v.contract_end IS 'Contract end';
COMMENT ON COLUMN vendor_display_v.contract_months IS 'Contract length (months)';
COMMENT ON COLUMN vendor_display_v.auto_renew IS 'Auto-renew';
COMMENT ON COLUMN vendor_display_v.renewal_rate IS 'Renewal rate';
COMMENT ON COLUMN vendor_display_v.renewal_notice IS 'Renewal notice (days)';
COMMENT ON COLUMN vendor_display_v.termination_terms IS 'Termination terms';
COMMENT ON COLUMN vendor_display_v.created_at IS 'Created';
COMMENT ON COLUMN vendor_display_v.modified_at IS 'Modified';
COMMENT ON COLUMN vendor_display_v.synced_at IS 'Last synced';
COMMENT ON COLUMN vendor_display_v.department IS 'Department';
COMMENT ON COLUMN vendor_display_v.owner IS 'Owner';
COMMENT ON COLUMN vendor_display_v.secondary_owner IS 'Secondary owner';
