BEGIN;

ALTER TABLE site_feedback
    ADD COLUMN collected BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE agent_feedback
    ADD COLUMN collected BOOLEAN NOT NULL DEFAULT FALSE;

CREATE INDEX idx_site_feedback_uncollected ON site_feedback (created_at) WHERE NOT collected;
CREATE INDEX idx_agent_feedback_uncollected ON agent_feedback (created_at) WHERE NOT collected;

COMMIT;
