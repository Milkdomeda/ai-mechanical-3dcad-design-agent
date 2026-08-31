CREATE TABLE IF NOT EXISTS knowledge_projection_state (
    projection_name text PRIMARY KEY,
    last_outbox_id bigint NOT NULL DEFAULT 0,
    rebuilt_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO knowledge_projection_state(projection_name)
VALUES ('neo4j')
ON CONFLICT (projection_name) DO NOTHING;

CREATE INDEX IF NOT EXISTS knowledge_outbox_pending_idx
    ON knowledge_outbox(id)
    WHERE projected_at IS NULL;

