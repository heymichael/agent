BEGIN;

CREATE TABLE site_feedback (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    user_id       UUID NOT NULL REFERENCES users(id),
    app_id        TEXT NOT NULL,
    open_panes    JSONB,
    feedback_text TEXT NOT NULL
);

CREATE INDEX idx_site_feedback_user ON site_feedback (user_id);

COMMIT;
