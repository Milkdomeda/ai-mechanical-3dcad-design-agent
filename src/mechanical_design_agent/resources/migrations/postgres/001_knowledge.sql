CREATE TABLE knowledge_schema_migrations (
    version integer PRIMARY KEY,
    filename text NOT NULL UNIQUE,
    sha256 char(64) NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    applied_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE product_families (
    id text PRIMARY KEY CHECK (btrim(id) <> ''),
    organization_id text NOT NULL CHECK (btrim(organization_id) <> ''),
    design_group_id text NOT NULL CHECK (btrim(design_group_id) <> ''),
    canonical_name text NOT NULL CHECK (btrim(canonical_name) <> ''),
    aliases text[] NOT NULL DEFAULT '{}',
    profile jsonb NOT NULL DEFAULT '{}'::jsonb,
    search_terms text[] NOT NULL DEFAULT '{}',
    search_text text NOT NULL CHECK (btrim(search_text) <> ''),
    status text NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'superseded', 'revoked')),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (organization_id, design_group_id, id)
);

CREATE TABLE knowledge_assertions (
    id text PRIMARY KEY CHECK (btrim(id) <> ''),
    organization_id text NOT NULL CHECK (btrim(organization_id) <> ''),
    design_group_id text NOT NULL CHECK (btrim(design_group_id) <> ''),
    product_family_id text,
    subject text NOT NULL CHECK (btrim(subject) <> ''),
    predicate text NOT NULL CHECK (btrim(predicate) <> ''),
    object_value jsonb NOT NULL,
    applicability jsonb NOT NULL DEFAULT '{}'::jsonb,
    evidence jsonb NOT NULL DEFAULT '[]'::jsonb,
    search_terms text[] NOT NULL DEFAULT '{}',
    search_text text NOT NULL CHECK (btrim(search_text) <> ''),
    status text NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'superseded', 'revoked')),
    supersedes_id text,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (organization_id, design_group_id, id),
    FOREIGN KEY (organization_id, design_group_id, product_family_id)
        REFERENCES product_families(organization_id, design_group_id, id),
    FOREIGN KEY (organization_id, design_group_id, supersedes_id)
        REFERENCES knowledge_assertions(organization_id, design_group_id, id),
    CHECK (supersedes_id IS NULL OR supersedes_id <> id)
);

CREATE TABLE design_lessons (
    id text PRIMARY KEY CHECK (btrim(id) <> ''),
    organization_id text NOT NULL CHECK (btrim(organization_id) <> ''),
    design_group_id text NOT NULL CHECK (btrim(design_group_id) <> ''),
    product_family_id text,
    content jsonb NOT NULL,
    applicability jsonb NOT NULL DEFAULT '{}'::jsonb,
    provenance jsonb NOT NULL DEFAULT '{}'::jsonb,
    search_terms text[] NOT NULL DEFAULT '{}',
    search_text text NOT NULL CHECK (btrim(search_text) <> ''),
    status text NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'superseded', 'revoked')),
    supersedes_id text,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (organization_id, design_group_id, id),
    FOREIGN KEY (organization_id, design_group_id, product_family_id)
        REFERENCES product_families(organization_id, design_group_id, id),
    FOREIGN KEY (organization_id, design_group_id, supersedes_id)
        REFERENCES design_lessons(organization_id, design_group_id, id),
    CHECK (supersedes_id IS NULL OR supersedes_id <> id)
);

CREATE INDEX product_families_scope_idx
    ON product_families(organization_id, design_group_id, status);
CREATE INDEX product_families_terms_idx ON product_families USING gin(search_terms);
CREATE INDEX product_families_text_idx
    ON product_families USING gin(to_tsvector('simple', search_text));

CREATE INDEX knowledge_assertions_scope_idx
    ON knowledge_assertions(organization_id, design_group_id, product_family_id, status);
CREATE INDEX knowledge_assertions_terms_idx ON knowledge_assertions USING gin(search_terms);
CREATE INDEX knowledge_assertions_text_idx
    ON knowledge_assertions USING gin(to_tsvector('simple', search_text));

CREATE INDEX design_lessons_scope_idx
    ON design_lessons(organization_id, design_group_id, product_family_id, status);
CREATE INDEX design_lessons_terms_idx ON design_lessons USING gin(search_terms);
CREATE INDEX design_lessons_text_idx
    ON design_lessons USING gin(to_tsvector('simple', search_text));
