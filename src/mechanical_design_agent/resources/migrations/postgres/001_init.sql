CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS organizations (
    id text PRIMARY KEY,
    name text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS design_groups (
    id text PRIMARY KEY,
    organization_id text NOT NULL REFERENCES organizations(id),
    name text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS actors (
    id text PRIMARY KEY,
    organization_id text NOT NULL REFERENCES organizations(id),
    display_name text NOT NULL,
    role text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS product_families (
    id text PRIMARY KEY,
    organization_id text NOT NULL REFERENCES organizations(id),
    design_group_id text NOT NULL REFERENCES design_groups(id),
    canonical_name text NOT NULL,
    aliases jsonb NOT NULL DEFAULT '[]'::jsonb,
    status text NOT NULL,
    config jsonb NOT NULL,
    revision integer NOT NULL DEFAULT 1,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS product_families_name_trgm_idx ON product_families USING gin (canonical_name gin_trgm_ops);

CREATE TABLE IF NOT EXISTS library_registrations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id text NOT NULL REFERENCES organizations(id),
    design_group_id text NOT NULL REFERENCES design_groups(id),
    root_path text NOT NULL UNIQUE,
    read_only boolean NOT NULL DEFAULT true,
    registered_by text NOT NULL REFERENCES actors(id),
    registered_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS family_folder_mappings (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    library_id uuid NOT NULL REFERENCES library_registrations(id) ON DELETE CASCADE,
    folder_name text NOT NULL,
    family_id text REFERENCES product_families(id),
    status text NOT NULL DEFAULT 'pending_confirmation',
    confirmed_by text REFERENCES actors(id),
    confirmed_at timestamptz,
    UNIQUE(library_id, folder_name)
);

CREATE TABLE IF NOT EXISTS library_files (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    library_id uuid NOT NULL REFERENCES library_registrations(id) ON DELETE CASCADE,
    relative_path text NOT NULL,
    absolute_path text NOT NULL,
    family_folder text NOT NULL,
    sha256 char(64) NOT NULL,
    size_bytes bigint NOT NULL,
    modified_at_ns bigint NOT NULL,
    suffix text NOT NULL,
    ingestion_status text NOT NULL DEFAULT 'pending_new',
    model_revision_id uuid,
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    missing_at timestamptz,
    UNIQUE(library_id, relative_path)
);
CREATE INDEX IF NOT EXISTS library_files_sha_idx ON library_files(library_id, sha256);

CREATE TABLE IF NOT EXISTS library_file_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    library_id uuid NOT NULL REFERENCES library_registrations(id) ON DELETE CASCADE,
    event_kind text NOT NULL,
    relative_path text NOT NULL,
    previous_path text,
    previous_sha256 char(64),
    current_sha256 char(64),
    details jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS library_file_events_path_idx ON library_file_events(library_id,relative_path,created_at);

CREATE TABLE IF NOT EXISTS artifacts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id text NOT NULL REFERENCES organizations(id),
    sha256 char(64) NOT NULL,
    size_bytes bigint NOT NULL,
    media_type text NOT NULL,
    storage_path text NOT NULL,
    source_path text,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(organization_id, sha256)
);

CREATE TABLE IF NOT EXISTS products (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id text NOT NULL REFERENCES organizations(id),
    design_group_id text NOT NULL REFERENCES design_groups(id),
    family_id text REFERENCES product_families(id),
    canonical_name text NOT NULL,
    aliases jsonb NOT NULL DEFAULT '[]'::jsonb,
    identity_assertion_id uuid,
    status text NOT NULL DEFAULT 'candidate',
    created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE products ADD COLUMN IF NOT EXISTS aliases jsonb NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE products ADD COLUMN IF NOT EXISTS identity_assertion_id uuid;

CREATE TABLE IF NOT EXISTS model_revisions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id text NOT NULL REFERENCES organizations(id),
    design_group_id text NOT NULL REFERENCES design_groups(id),
    family_id text REFERENCES product_families(id),
    product_id uuid REFERENCES products(id),
    source_artifact_id uuid NOT NULL REFERENCES artifacts(id),
    source_relative_path text NOT NULL,
    family_folder text NOT NULL,
    revision_number integer NOT NULL DEFAULT 1,
    previous_revision_id uuid REFERENCES model_revisions(id),
    parser_version text NOT NULL,
    status text NOT NULL,
    manifest jsonb NOT NULL,
    geometry_vector vector(64),
    structure_vector vector(32),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(source_artifact_id, parser_version)
);
ALTER TABLE model_revisions ADD COLUMN IF NOT EXISTS previous_revision_id uuid REFERENCES model_revisions(id);
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'library_files_model_revision_fk'
    ) THEN
        ALTER TABLE library_files
            ADD CONSTRAINT library_files_model_revision_fk
            FOREIGN KEY (model_revision_id) REFERENCES model_revisions(id);
    END IF;
END
$$;
CREATE INDEX IF NOT EXISTS model_revisions_family_idx ON model_revisions(family_id, status);
CREATE INDEX IF NOT EXISTS model_geometry_hnsw_idx ON model_revisions USING hnsw (geometry_vector vector_cosine_ops);
CREATE INDEX IF NOT EXISTS model_structure_hnsw_idx ON model_revisions USING hnsw (structure_vector vector_cosine_ops);

CREATE TABLE IF NOT EXISTS product_subfamilies (
    id text PRIMARY KEY,
    family_id text NOT NULL REFERENCES product_families(id),
    canonical_name text NOT NULL,
    aliases jsonb NOT NULL DEFAULT '[]'::jsonb,
    status text NOT NULL DEFAULT 'proposed',
    evidence jsonb NOT NULL,
    created_by text NOT NULL REFERENCES actors(id),
    approved_by text REFERENCES actors(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    reviewed_at timestamptz
);

CREATE TABLE IF NOT EXISTS model_subfamily_assignments (
    model_revision_id uuid NOT NULL REFERENCES model_revisions(id) ON DELETE CASCADE,
    subfamily_id text NOT NULL REFERENCES product_subfamilies(id) ON DELETE CASCADE,
    status text NOT NULL,
    confirmed_by text REFERENCES actors(id),
    confirmed_at timestamptz,
    PRIMARY KEY(model_revision_id,subfamily_id)
);

CREATE TABLE IF NOT EXISTS source_nodes (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    model_revision_id uuid NOT NULL REFERENCES model_revisions(id) ON DELETE CASCADE,
    source_id text NOT NULL,
    parent_source_id text,
    node_kind text NOT NULL,
    source_name text NOT NULL,
    source_label text NOT NULL,
    payload jsonb NOT NULL,
    UNIQUE(model_revision_id, source_id)
);

CREATE TABLE IF NOT EXISTS structure_hypotheses (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    model_revision_id uuid NOT NULL REFERENCES model_revisions(id) ON DELETE CASCADE,
    hypothesis_kind text NOT NULL,
    subject_source_id text NOT NULL,
    object_source_id text,
    confidence double precision NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    status text NOT NULL DEFAULT 'inferred_candidate',
    evidence jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ingestion_jobs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    library_id uuid NOT NULL REFERENCES library_registrations(id),
    status text NOT NULL,
    selection jsonb NOT NULL,
    result jsonb,
    error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    completed_at timestamptz
);

CREATE TABLE IF NOT EXISTS interaction_sessions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id text NOT NULL REFERENCES organizations(id),
    design_group_id text NOT NULL REFERENCES design_groups(id),
    family_id text REFERENCES product_families(id),
    model_revision_id uuid REFERENCES model_revisions(id),
    agent_runtime text NOT NULL DEFAULT 'codex-current',
    status text NOT NULL DEFAULT 'active',
    created_by text NOT NULL REFERENCES actors(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    closed_at timestamptz
);

CREATE TABLE IF NOT EXISTS question_items (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id uuid NOT NULL REFERENCES interaction_sessions(id) ON DELETE CASCADE,
    question_kind text NOT NULL,
    prompt_intent text NOT NULL,
    target_refs jsonb NOT NULL,
    evidence jsonb NOT NULL,
    score double precision NOT NULL,
    status text NOT NULL DEFAULT 'open',
    deferred_reason text,
    created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE question_items ADD COLUMN IF NOT EXISTS deferred_reason text;
ALTER TABLE question_items ADD COLUMN IF NOT EXISTS prompt_intent text;
UPDATE question_items SET prompt_intent = question_kind WHERE prompt_intent IS NULL;
ALTER TABLE question_items ALTER COLUMN prompt_intent SET NOT NULL;

CREATE TABLE IF NOT EXISTS answer_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id uuid NOT NULL REFERENCES interaction_sessions(id) ON DELETE CASCADE,
    question_ids jsonb NOT NULL,
    engineer_text text NOT NULL,
    agent_interpretation jsonb NOT NULL DEFAULT '{}'::jsonb,
    actor_id text NOT NULL REFERENCES actors(id),
    content_sha256 char(64) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(session_id, content_sha256)
);

CREATE TABLE IF NOT EXISTS knowledge_assertions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id text NOT NULL REFERENCES organizations(id),
    design_group_id text NOT NULL REFERENCES design_groups(id),
    family_id text REFERENCES product_families(id),
    product_id uuid REFERENCES products(id),
    model_revision_id uuid REFERENCES model_revisions(id),
    interaction_session_id uuid REFERENCES interaction_sessions(id),
    subject_ref text NOT NULL,
    predicate text NOT NULL,
    object_value jsonb NOT NULL,
    unit text,
    scope_kind text NOT NULL,
    risk_level text NOT NULL,
    status text NOT NULL,
    source_kind text NOT NULL,
    evidence jsonb NOT NULL,
    confidence double precision NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    applicability jsonb NOT NULL DEFAULT '{}'::jsonb,
    non_applicable_conditions jsonb NOT NULL DEFAULT '[]'::jsonb,
    contradicts jsonb NOT NULL DEFAULT '[]'::jsonb,
    supersedes uuid REFERENCES knowledge_assertions(id),
    revision integer NOT NULL DEFAULT 1,
    created_by text NOT NULL REFERENCES actors(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'products_identity_assertion_fk'
    ) THEN
        ALTER TABLE products
            ADD CONSTRAINT products_identity_assertion_fk
            FOREIGN KEY (identity_assertion_id) REFERENCES knowledge_assertions(id);
    END IF;
END
$$;
CREATE INDEX IF NOT EXISTS assertions_scope_idx ON knowledge_assertions(organization_id, design_group_id, family_id, status);
CREATE INDEX IF NOT EXISTS assertions_predicate_trgm_idx ON knowledge_assertions USING gin (predicate gin_trgm_ops);

CREATE TABLE IF NOT EXISTS knowledge_search_documents (
    assertion_id uuid PRIMARY KEY REFERENCES knowledge_assertions(id) ON DELETE CASCADE,
    organization_id text NOT NULL,
    design_group_id text NOT NULL,
    family_id text,
    exact_terms text[] NOT NULL DEFAULT '{}',
    search_text text NOT NULL,
    search_vector tsvector GENERATED ALWAYS AS (to_tsvector('simple', search_text)) STORED,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS knowledge_search_exact_idx ON knowledge_search_documents USING gin (exact_terms);
CREATE INDEX IF NOT EXISTS knowledge_search_fts_idx ON knowledge_search_documents USING gin (search_vector);
CREATE INDEX IF NOT EXISTS knowledge_search_trgm_idx ON knowledge_search_documents USING gin (search_text gin_trgm_ops);

CREATE TABLE IF NOT EXISTS review_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    assertion_id uuid NOT NULL REFERENCES knowledge_assertions(id),
    decision text NOT NULL,
    reviewer_id text NOT NULL REFERENCES actors(id),
    reviewer_text text NOT NULL,
    previous_status text NOT NULL,
    resulting_status text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS family_profiles (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    family_id text NOT NULL REFERENCES product_families(id),
    revision integer NOT NULL,
    status text NOT NULL,
    distinct_model_count integer NOT NULL,
    profile jsonb NOT NULL,
    evidence jsonb NOT NULL,
    created_by text NOT NULL REFERENCES actors(id),
    approved_by text REFERENCES actors(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(family_id, revision)
);

CREATE TABLE IF NOT EXISTS design_working_copies (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id text NOT NULL REFERENCES organizations(id),
    design_group_id text NOT NULL REFERENCES design_groups(id),
    family_id text REFERENCES product_families(id),
    source_model_revision_id uuid REFERENCES model_revisions(id),
    source_sha256 char(64) NOT NULL,
    source_kind text NOT NULL DEFAULT 'existing_model',
    working_path text NOT NULL,
    status text NOT NULL DEFAULT 'draft',
    created_by text NOT NULL REFERENCES actors(id),
    created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE design_working_copies ADD COLUMN IF NOT EXISTS source_kind text NOT NULL DEFAULT 'existing_model';

CREATE TABLE IF NOT EXISTS design_change_sets (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    working_copy_id uuid NOT NULL REFERENCES design_working_copies(id),
    status text NOT NULL DEFAULT 'proposed',
    change_phase text NOT NULL,
    changes jsonb NOT NULL,
    knowledge_used jsonb NOT NULL DEFAULT '[]'::jsonb,
    rationale text NOT NULL,
    created_by text NOT NULL REFERENCES actors(id),
    reviewed_by text REFERENCES actors(id),
    review_text text,
    reviewed_at timestamptz,
    applied_at timestamptz,
    resulting_sha256 char(64),
    created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE design_change_sets ADD COLUMN IF NOT EXISTS change_phase text;
ALTER TABLE design_change_sets ADD COLUMN IF NOT EXISTS reviewed_by text REFERENCES actors(id);
ALTER TABLE design_change_sets ADD COLUMN IF NOT EXISTS review_text text;
ALTER TABLE design_change_sets ADD COLUMN IF NOT EXISTS reviewed_at timestamptz;
ALTER TABLE design_change_sets ADD COLUMN IF NOT EXISTS applied_at timestamptz;
ALTER TABLE design_change_sets ADD COLUMN IF NOT EXISTS resulting_sha256 char(64);

CREATE TABLE IF NOT EXISTS validation_reports (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    working_copy_id uuid NOT NULL REFERENCES design_working_copies(id),
    change_set_id uuid REFERENCES design_change_sets(id),
    status text NOT NULL,
    checks jsonb NOT NULL,
    working_sha256 char(64) NOT NULL,
    report_path text,
    validation_kind text NOT NULL DEFAULT 'geometry_model',
    created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE validation_reports ADD COLUMN IF NOT EXISTS working_sha256 char(64);
ALTER TABLE validation_reports ADD COLUMN IF NOT EXISTS validation_kind text NOT NULL DEFAULT 'geometry_model';
CREATE INDEX IF NOT EXISTS validation_reports_kind_idx ON validation_reports(working_copy_id, validation_kind, created_at DESC);

CREATE TABLE IF NOT EXISTS standard_part_records (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    provider_id text NOT NULL,
    provider_name text NOT NULL,
    trust_tier text NOT NULL,
    part_number text NOT NULL,
    standard text NOT NULL,
    nominal_size text NOT NULL,
    source_url text NOT NULL,
    sha256 char(64) NOT NULL,
    local_path text NOT NULL,
    manifest_path text NOT NULL,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    approval_reference text NOT NULL,
    validation_report_path text NOT NULL,
    approved_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(provider_id, part_number, sha256)
);
ALTER TABLE standard_part_records ADD COLUMN IF NOT EXISTS approval_reference text;
ALTER TABLE standard_part_records ADD COLUMN IF NOT EXISTS validation_report_path text;
ALTER TABLE standard_part_records ADD COLUMN IF NOT EXISTS approved_at timestamptz NOT NULL DEFAULT now();
CREATE INDEX IF NOT EXISTS standard_part_records_part_idx ON standard_part_records(part_number);

CREATE TABLE IF NOT EXISTS outbox_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    aggregate_type text NOT NULL,
    aggregate_id text NOT NULL,
    event_type text NOT NULL,
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    processed_at timestamptz,
    attempts integer NOT NULL DEFAULT 0,
    last_error text
);
CREATE INDEX IF NOT EXISTS outbox_pending_idx ON outbox_events(processed_at, created_at);
