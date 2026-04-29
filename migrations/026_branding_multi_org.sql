-- 026_branding_multi_org.sql
-- Add org_slug to branding table, removing the singleton constraint.
-- Each org can have its own logo, lockup, and lockup mode.

BEGIN;

-- Idempotency guard: skip if org_slug column already exists
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'branding' AND column_name = 'org_slug'
    ) THEN
        RAISE NOTICE 'branding.org_slug already exists, skipping migration';
        RETURN;
    END IF;

    -- Step 1: Drop the singleton constraint (CHECK id = 1)
    ALTER TABLE branding DROP CONSTRAINT IF EXISTS branding_id_check;

    -- Step 2: Add org_slug column (nullable first for backfill)
    ALTER TABLE branding ADD COLUMN org_slug TEXT;

    -- Step 3: Backfill existing row to haderach
    UPDATE branding SET org_slug = 'haderach' WHERE id = 1;

    -- Step 4: Make org_slug NOT NULL, add FK and UNIQUE constraint
    ALTER TABLE branding
        ALTER COLUMN org_slug SET NOT NULL,
        ADD CONSTRAINT branding_org_slug_fk FOREIGN KEY (org_slug) REFERENCES orgs(slug),
        ADD CONSTRAINT branding_org_slug_unique UNIQUE (org_slug);

    -- Step 5: Create index for efficient lookups
    CREATE INDEX branding_org_slug_idx ON branding (org_slug);

END $$;

COMMIT;
