CREATE TABLE IF NOT EXISTS schema_migrations (
    version integer PRIMARY KEY,
    filename text NOT NULL UNIQUE,
    sha256 char(64) NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS design_lesson_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    lesson_key text NOT NULL,
    revision integer NOT NULL,
    organization_id text NOT NULL REFERENCES organizations(id),
    source_design_group_id text NOT NULL REFERENCES design_groups(id),
    source_family_id text REFERENCES product_families(id),
    source_working_copy_id uuid REFERENCES design_working_copies(id),
    codex_session_id text NOT NULL,
    title text NOT NULL,
    before_model_sha256 char(64) NOT NULL,
    after_model_sha256 char(64) NOT NULL,
    problem jsonb NOT NULL,
    root_causes jsonb NOT NULL,
    corrections jsonb NOT NULL,
    prevention jsonb NOT NULL,
    applicability jsonb NOT NULL,
    non_applicable_conditions jsonb NOT NULL,
    search_terms text[] NOT NULL DEFAULT '{}',
    evidence_manifest jsonb NOT NULL,
    package_sha256 char(64) NOT NULL UNIQUE,
    archived_package_path text NOT NULL,
    status text NOT NULL CHECK (status IN ('approved','superseded','revoked')),
    supersedes uuid REFERENCES design_lesson_events(id),
    approved_by text NOT NULL REFERENCES actors(id),
    approval_text text NOT NULL,
    approved_at timestamptz NOT NULL DEFAULT now(),
    revoked_by text REFERENCES actors(id),
    revoked_reason text,
    revoked_at timestamptz,
    UNIQUE(organization_id, lesson_key, revision)
);

CREATE TABLE IF NOT EXISTS design_lesson_change_sets (
    lesson_event_id uuid NOT NULL REFERENCES design_lesson_events(id) ON DELETE CASCADE,
    change_set_id uuid NOT NULL REFERENCES design_change_sets(id),
    sort_order integer NOT NULL,
    PRIMARY KEY(lesson_event_id, change_set_id),
    UNIQUE(lesson_event_id, sort_order)
);

CREATE TABLE IF NOT EXISTS design_lesson_assertions (
    lesson_event_id uuid NOT NULL REFERENCES design_lesson_events(id) ON DELETE CASCADE,
    assertion_id uuid NOT NULL REFERENCES knowledge_assertions(id),
    assertion_key text NOT NULL,
    sort_order integer NOT NULL,
    PRIMARY KEY(lesson_event_id, assertion_id),
    UNIQUE(lesson_event_id, assertion_key)
);

CREATE INDEX IF NOT EXISTS design_lessons_active_idx
    ON design_lesson_events(organization_id,status,approved_at DESC);
CREATE INDEX IF NOT EXISTS design_lessons_search_terms_idx
    ON design_lesson_events USING gin(search_terms);
