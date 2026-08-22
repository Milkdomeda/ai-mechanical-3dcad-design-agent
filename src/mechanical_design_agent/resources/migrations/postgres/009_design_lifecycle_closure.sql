ALTER TABLE design_working_copies
    ADD COLUMN IF NOT EXISTS design_origin text;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'design_working_copies_origin_check'
    ) THEN
        ALTER TABLE design_working_copies
            ADD CONSTRAINT design_working_copies_origin_check
            CHECK (
                design_origin IS NULL
                OR design_origin IN ('existing_model', 'new_design')
            ) NOT VALID;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS design_retrieval_receipts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    working_copy_id uuid NOT NULL REFERENCES design_working_copies(id) ON DELETE CASCADE,
    design_origin text NOT NULL CHECK (design_origin IN ('existing_model', 'new_design')),
    source_model_revision_id uuid REFERENCES model_revisions(id),
    family_id text REFERENCES product_families(id),
    query text NOT NULL,
    retrieval_scope jsonb NOT NULL DEFAULT '{}'::jsonb,
    retrieved_knowledge_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
    used_knowledge_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
    retrieval_status text NOT NULL CHECK (
        retrieval_status IN ('completed', 'completed_no_match', 'not_executed')
    ),
    non_use_reason text,
    created_by text NOT NULL REFERENCES actors(id),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS design_retrieval_receipts_working_idx
    ON design_retrieval_receipts(working_copy_id, created_at DESC, id DESC);

ALTER TABLE design_change_sets
    ADD COLUMN IF NOT EXISTS superseded_by_change_set_id uuid REFERENCES design_change_sets(id);
ALTER TABLE design_change_sets
    ADD COLUMN IF NOT EXISTS closure_reason text;
ALTER TABLE design_change_sets
    ADD COLUMN IF NOT EXISTS closed_by text REFERENCES actors(id);
ALTER TABLE design_change_sets
    ADD COLUMN IF NOT EXISTS closed_at timestamptz;

CREATE TABLE IF NOT EXISTS design_lesson_summaries (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    working_copy_id uuid NOT NULL REFERENCES design_working_copies(id) ON DELETE CASCADE,
    summary jsonb NOT NULL,
    summary_status text NOT NULL DEFAULT 'completed' CHECK (summary_status = 'completed'),
    publication_status text NOT NULL CHECK (publication_status IN ('ready', 'blocked')),
    publication_blocker text,
    created_by text NOT NULL REFERENCES actors(id),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS design_lesson_summaries_working_idx
    ON design_lesson_summaries(working_copy_id, created_at DESC, id DESC);
