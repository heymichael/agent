-- 016_home_investor_roles.sql
-- Add roles for the haderach-home marketing site.
-- 'home'     — grants access to public marketing pages (homepage, blog, careers, team).
-- 'investor' — grants access to the role-gated investors page.

INSERT INTO roles (name) VALUES ('home')     ON CONFLICT (name) DO NOTHING;
INSERT INTO roles (name) VALUES ('investor') ON CONFLICT (name) DO NOTHING;
