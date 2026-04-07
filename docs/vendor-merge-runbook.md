# Vendor Merge Runbook

When two vendor records represent the same real-world entity (e.g. "Interexy"
and "Interexy LLC"), one should be hidden and its spend data reassigned to the
surviving record so aggregate queries report a single, correct total.

## Prerequisites

- Identify the **target** vendor (the one that will survive) and the **source**
  vendor (the duplicate being retired). Grab both UUIDs from the `vendors`
  table.
- If the source vendor is still being synced (`sync_billcom_spend`,
  `sync_gcp_spend`, `sync_aws_spend`), disable or redirect the sync **before**
  running this migration — otherwise the next sync recreates spend rows under
  the source vendor.
- Run everything in a single transaction so failures roll back atomically.

## Migration SQL

Replace `<SOURCE_VENDOR_ID>` and `<TARGET_VENDOR_ID>` with the actual UUIDs.

```sql
BEGIN;

-- 1. Reassign monthly spend summaries.
--    Merge amounts for months where both vendors have data.
INSERT INTO vendor_monthly_spend (vendor_id, date, total_amount, bill_count, synced_at)
SELECT
    '<TARGET_VENDOR_ID>'::uuid,
    s.date,
    s.total_amount,
    s.bill_count,
    now()
FROM vendor_monthly_spend s
WHERE s.vendor_id = '<SOURCE_VENDOR_ID>'::uuid
ON CONFLICT (vendor_id, date)
DO UPDATE SET
    total_amount = vendor_monthly_spend.total_amount + EXCLUDED.total_amount,
    bill_count   = vendor_monthly_spend.bill_count   + EXCLUDED.bill_count,
    synced_at    = now();

DELETE FROM vendor_monthly_spend
WHERE vendor_id = '<SOURCE_VENDOR_ID>'::uuid;

-- 2. Reassign granular spend detail rows.
--    The composite unique index (vendor_id, date, category, subcategory,
--    project, user_email) makes collisions unlikely across source systems.
--    If a conflict occurs, resolve the overlapping rows manually first.
UPDATE vendor_spend_detail
SET vendor_id = '<TARGET_VENDOR_ID>'::uuid,
    synced_at = now()
WHERE vendor_id = '<SOURCE_VENDOR_ID>'::uuid;

-- 3. Hide the source vendor (idempotent).
UPDATE vendors
SET hidden_from_agent = true,
    modified_at = now()
WHERE id = '<SOURCE_VENDOR_ID>'::uuid;

COMMIT;
```

## Verification

```sql
-- Source vendor should have zero spend rows.
SELECT count(*) FROM vendor_monthly_spend  WHERE vendor_id = '<SOURCE_VENDOR_ID>'::uuid;
SELECT count(*) FROM vendor_spend_detail   WHERE vendor_id = '<SOURCE_VENDOR_ID>'::uuid;

-- Target vendor totals should reflect the combined spend.
SELECT date, total_amount, bill_count
FROM vendor_monthly_spend
WHERE vendor_id = '<TARGET_VENDOR_ID>'::uuid
ORDER BY date;
```

## Notes

- **Access grants**: if the source vendor had rows in `user_contractor_access`
  or `user_allowed_vendors`, decide whether those grants should carry over to
  the target vendor and migrate them manually if so.
- **Rollback**: the entire migration runs in one transaction, so a failure at
  any step rolls back all changes automatically.
