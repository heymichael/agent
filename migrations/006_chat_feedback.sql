BEGIN;

CREATE TABLE chat_sessions (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    modified_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    user_id       UUID NOT NULL REFERENCES users(id),
    app_context   TEXT NOT NULL,
    messages      JSONB NOT NULL DEFAULT '[]'::jsonb
);

CREATE INDEX idx_chat_sessions_user ON chat_sessions (user_id);

CREATE TABLE agent_feedback (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    modified_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    user_id         UUID NOT NULL REFERENCES users(id),
    chat_session_id UUID NOT NULL REFERENCES chat_sessions(id),
    message_seq     INT NOT NULL,
    signal          BOOLEAN NOT NULL,
    comment         TEXT,
    UNIQUE (chat_session_id, message_seq)
);

CREATE INDEX idx_agent_feedback_session ON agent_feedback (chat_session_id);

COMMIT;
