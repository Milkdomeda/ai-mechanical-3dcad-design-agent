CREATE EXTENSION IF NOT EXISTS vector;

ALTER TABLE knowledge_assertions
    ADD COLUMN IF NOT EXISTS search_document tsvector
    GENERATED ALWAYS AS (
        to_tsvector('simple', coalesce(subject, '') || ' ' || coalesce(predicate, ''))
    ) STORED;

ALTER TABLE knowledge_assertions
    ADD COLUMN IF NOT EXISTS embedding vector(1536);

ALTER TABLE design_lessons
    ADD COLUMN IF NOT EXISTS search_document tsvector
    GENERATED ALWAYS AS (
        to_tsvector('simple', array_to_string(search_terms, ' '))
    ) STORED;

ALTER TABLE design_lessons
    ADD COLUMN IF NOT EXISTS embedding vector(1536);

CREATE INDEX IF NOT EXISTS knowledge_assertions_search_idx
    ON knowledge_assertions USING gin(search_document);
CREATE INDEX IF NOT EXISTS design_lessons_search_idx
    ON design_lessons USING gin(search_document);
CREATE INDEX IF NOT EXISTS knowledge_assertions_scope_idx
    ON knowledge_assertions(organization_id, design_group_id, product_family_id, status);
CREATE INDEX IF NOT EXISTS design_lessons_scope_idx
    ON design_lessons(organization_id, design_group_id, product_family_id, status);

