-- 001_init.sql
-- Full schema for the Firestore-to-Postgres migration.
-- Run once against a fresh database before deploying the new service code.

BEGIN;

-- Reference tables ---------------------------------------------------------

CREATE TABLE departments (
  id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name  TEXT NOT NULL UNIQUE
);

CREATE TABLE roles (
  id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name  TEXT NOT NULL UNIQUE
);

-- Core tables --------------------------------------------------------------

CREATE TABLE users (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  email       TEXT NOT NULL UNIQUE,
  first_name  TEXT NOT NULL DEFAULT '',
  last_name   TEXT NOT NULL DEFAULT '',
  created_at  TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE vendors (
  id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  source_system      TEXT NOT NULL,
  source_system_id   TEXT NOT NULL,
  name               TEXT NOT NULL,
  department_id      UUID REFERENCES departments(id),
  owner_id           UUID REFERENCES users(id),
  secondary_owner_id UUID REFERENCES users(id),
  payment_method     TEXT,
  billing_frequency  TEXT,
  account_type       TEXT,
  track_1099         BOOLEAN DEFAULT FALSE,
  purpose            TEXT,
  spend_type         TEXT,
  aliases            TEXT[],
  contract_start     DATE,
  contract_end       DATE,
  contract_months    INT,
  auto_renew         BOOLEAN,
  renewal_rate       TEXT,
  renewal_notice     INT,
  termination_terms  TEXT,
  created_at         TIMESTAMPTZ DEFAULT now(),
  modified_at        TIMESTAMPTZ DEFAULT now(),
  synced_at          TIMESTAMPTZ,
  UNIQUE (source_system, source_system_id)
);

CREATE TABLE vendor_monthly_spend (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  vendor_id    UUID NOT NULL REFERENCES vendors(id),
  date         DATE NOT NULL,
  total_amount NUMERIC(12,2) NOT NULL,
  bill_count   INT NOT NULL DEFAULT 1,
  synced_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (vendor_id, date)
);

CREATE TABLE apps (
  id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  slug       TEXT NOT NULL UNIQUE,
  label      TEXT,
  type       TEXT NOT NULL DEFAULT 'app',
  sort_order INT NOT NULL DEFAULT 99,
  path       TEXT,
  icon       TEXT
);

-- Join tables --------------------------------------------------------------

CREATE TABLE user_roles (
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  role_id UUID NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
  PRIMARY KEY (user_id, role_id)
);

CREATE TABLE user_allowed_departments (
  user_id       UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  department_id UUID NOT NULL REFERENCES departments(id) ON DELETE CASCADE,
  PRIMARY KEY (user_id, department_id)
);

CREATE TABLE user_allowed_vendors (
  user_id   UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  vendor_id UUID NOT NULL REFERENCES vendors(id) ON DELETE CASCADE,
  PRIMARY KEY (user_id, vendor_id)
);

CREATE TABLE user_denied_vendors (
  user_id   UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  vendor_id UUID NOT NULL REFERENCES vendors(id) ON DELETE CASCADE,
  PRIMARY KEY (user_id, vendor_id)
);

CREATE TABLE app_granting_roles (
  app_id  UUID NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
  role_id UUID NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
  PRIMARY KEY (app_id, role_id)
);

-- Indexes ------------------------------------------------------------------

CREATE INDEX idx_spend_date ON vendor_monthly_spend(date);
CREATE INDEX idx_vendors_name_lower ON vendors (LOWER(name));
CREATE INDEX idx_vendors_source ON vendors (source_system, source_system_id);

-- Seed roles ---------------------------------------------------------------

INSERT INTO roles (name) VALUES
  ('admin'),
  ('finance_admin'),
  ('user'),
  ('haderach_user');

COMMIT;
