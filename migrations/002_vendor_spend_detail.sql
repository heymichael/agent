-- 002_vendor_spend_detail.sql
-- Adds the vendor_spend_detail table for granular, per-line-item spend data.
-- Complements vendor_monthly_spend (rolled-up summary) with breakdowns by
-- service/category, SKU/subcategory, project, and optionally user.
--
-- Each vendor's sync job maps its native fields into the canonical columns;
-- vendor-specific extras go in the metadata JSONB column.

BEGIN;

CREATE TABLE vendor_spend_detail (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  vendor_id    UUID NOT NULL REFERENCES vendors(id),
  date         DATE NOT NULL,
  amount       NUMERIC(12,2) NOT NULL,
  category     TEXT,
  subcategory  TEXT,
  project      TEXT,
  user_email   TEXT,
  metadata     JSONB DEFAULT '{}',
  synced_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX idx_spend_detail_unique ON vendor_spend_detail (
  vendor_id, date,
  COALESCE(category, ''),
  COALESCE(subcategory, ''),
  COALESCE(project, ''),
  COALESCE(user_email, '')
);

CREATE INDEX idx_spend_detail_vendor_date ON vendor_spend_detail(vendor_id, date);
CREATE INDEX idx_spend_detail_category ON vendor_spend_detail(category);
CREATE INDEX idx_spend_detail_subcategory ON vendor_spend_detail(subcategory);

COMMIT;
