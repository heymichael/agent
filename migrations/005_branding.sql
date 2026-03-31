BEGIN;

CREATE TYPE lockup_mode AS ENUM ('none', 'text', 'swap');

CREATE TABLE branding (
    id    SERIAL PRIMARY KEY CHECK (id = 1),
    logo_svg    TEXT,
    lockup_svg  TEXT,
    show_lockup lockup_mode NOT NULL DEFAULT 'none'
);

COMMIT;
