CREATE TABLE IF NOT EXISTS design_lesson_reviews (
    id text PRIMARY KEY CHECK (id ~ '^DLR-[A-Za-z0-9-]+$'),
    organization_id text NOT NULL REFERENCES organizations(id),
    design_group_id text NOT NULL REFERENCES design_groups(id),
    working_copy_id uuid NOT NULL REFERENCES design_working_copies(id),
    lesson_id text NOT NULL,
    package_sha256 char(64) NOT NULL UNIQUE,
    review_card_sha256 char(64) NOT NULL,
    final_model_sha256 char(64) NOT NULL,
    status text NOT NULL CHECK (status IN (
        'awaiting-engineer-review','superseded','rejected','invalid',
        'approved-retrieval-pending','stored-and-retrievable'
    )),
    review_path text NOT NULL,
    package_path text NOT NULL,
    created_by text NOT NULL REFERENCES actors(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    reviewed_by text REFERENCES actors(id),
    reviewed_at timestamptz,
    reviewer_text text,
    supersedes_review_id text REFERENCES design_lesson_reviews(id),
    published_design_lesson_id uuid REFERENCES design_lesson_events(id),
    retrieval_probe jsonb,
    retrieval_verified_at timestamptz
);

CREATE INDEX IF NOT EXISTS design_lesson_reviews_working_copy_idx
    ON design_lesson_reviews(working_copy_id, created_at DESC);
CREATE INDEX IF NOT EXISTS design_lesson_reviews_status_idx
    ON design_lesson_reviews(status, created_at);
