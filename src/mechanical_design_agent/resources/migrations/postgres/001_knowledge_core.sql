CREATE TABLE IF NOT EXISTS knowledge_schema_migrations (
    version integer PRIMARY KEY,
    filename text NOT NULL UNIQUE,
    sha256 char(64) NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS organizations (
    id text PRIMARY KEY,
    name text NOT NULL
);

CREATE TABLE IF NOT EXISTS design_groups (
    id text PRIMARY KEY,
    organization_id text NOT NULL REFERENCES organizations(id),
    name text NOT NULL,
    UNIQUE (id, organization_id)
);

CREATE TABLE IF NOT EXISTS product_families (
    id text PRIMARY KEY,
    organization_id text NOT NULL REFERENCES organizations(id),
    design_group_id text NOT NULL,
    canonical_name text NOT NULL,
    aliases jsonb NOT NULL DEFAULT '[]'::jsonb,
    knowledge jsonb NOT NULL DEFAULT '{}'::jsonb,
    status text NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'superseded', 'revoked')),
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (design_group_id, organization_id)
        REFERENCES design_groups(id, organization_id),
    UNIQUE (id, organization_id, design_group_id)
);

CREATE TABLE IF NOT EXISTS knowledge_assertions (
    id text PRIMARY KEY,
    organization_id text NOT NULL REFERENCES organizations(id),
    design_group_id text NOT NULL,
    product_family_id text,
    subject text NOT NULL,
    predicate text NOT NULL,
    object_value jsonb NOT NULL,
    applicability jsonb NOT NULL DEFAULT '{}'::jsonb,
    evidence jsonb NOT NULL DEFAULT '[]'::jsonb,
    authorization jsonb NOT NULL DEFAULT '{}'::jsonb,
    status text NOT NULL DEFAULT 'approved'
        CHECK (status IN ('approved', 'superseded', 'revoked')),
    supersedes_id text REFERENCES knowledge_assertions(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (design_group_id, organization_id)
        REFERENCES design_groups(id, organization_id),
    FOREIGN KEY (product_family_id, organization_id, design_group_id)
        REFERENCES product_families(id, organization_id, design_group_id)
);

CREATE TABLE IF NOT EXISTS design_lesson_reviews (
    review_sha256 char(64) PRIMARY KEY,
    organization_id text NOT NULL REFERENCES organizations(id),
    design_group_id text NOT NULL,
    product_family_id text,
    review_card jsonb NOT NULL,
    decision text NOT NULL CHECK (decision IN ('approved', 'declined')),
    decision_text text NOT NULL,
    decided_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (design_group_id, organization_id)
        REFERENCES design_groups(id, organization_id),
    FOREIGN KEY (product_family_id, organization_id, design_group_id)
        REFERENCES product_families(id, organization_id, design_group_id)
);

CREATE TABLE IF NOT EXISTS design_lessons (
    id text PRIMARY KEY,
    review_sha256 char(64) NOT NULL REFERENCES design_lesson_reviews(review_sha256),
    organization_id text NOT NULL REFERENCES organizations(id),
    design_group_id text NOT NULL,
    product_family_id text,
    lesson jsonb NOT NULL,
    search_terms text[] NOT NULL DEFAULT '{}',
    applicability jsonb NOT NULL DEFAULT '{}'::jsonb,
    status text NOT NULL DEFAULT 'approved'
        CHECK (status IN ('approved', 'superseded', 'revoked')),
    supersedes_id text REFERENCES design_lessons(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (design_group_id, organization_id)
        REFERENCES design_groups(id, organization_id),
    FOREIGN KEY (product_family_id, organization_id, design_group_id)
        REFERENCES product_families(id, organization_id, design_group_id),
    UNIQUE (review_sha256, id)
);

CREATE TABLE IF NOT EXISTS knowledge_review_decisions (
    id text PRIMARY KEY,
    organization_id text NOT NULL REFERENCES organizations(id),
    subject_type text NOT NULL CHECK (
        subject_type IN ('product_family', 'assertion', 'design_lesson')
    ),
    subject_id text NOT NULL,
    decision text NOT NULL CHECK (decision IN ('approved', 'rejected')),
    decision_text text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS knowledge_outbox (
    id bigserial PRIMARY KEY,
    aggregate_type text NOT NULL CHECK (
        aggregate_type IN ('product_family', 'assertion', 'design_lesson')
    ),
    aggregate_id text NOT NULL,
    event_type text NOT NULL,
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    projected_at timestamptz,
    UNIQUE (aggregate_type, aggregate_id, event_type)
);
