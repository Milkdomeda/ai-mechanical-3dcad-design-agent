CREATE TABLE IF NOT EXISTS product_family_onboarding_runs (
    id uuid PRIMARY KEY,
    job_id uuid NOT NULL,
    organization_id text NOT NULL,
    design_group_id text NOT NULL,
    family_id text NOT NULL,
    status text NOT NULL CHECK (
        status IN ('started','analyzed','approved','rejected','published')
    ),
    input_manifest jsonb NOT NULL,
    input_manifest_sha256 char(64) NOT NULL CHECK (
        input_manifest_sha256 ~ '^[0-9a-f]{64}$'
    ),
    analysis jsonb,
    analysis_sha256 char(64),
    analysis_path text,
    candidate_knowledge jsonb,
    package_sha256 char(64),
    package_path text,
    started_job_revision integer NOT NULL CHECK (started_job_revision >= 0),
    analyzed_job_revision integer CHECK (analyzed_job_revision >= 0),
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    analyzed_at timestamptz,
    CONSTRAINT product_family_onboarding_runs_job_scope_fk
        FOREIGN KEY (job_id,organization_id,design_group_id)
        REFERENCES design_jobs(id,organization_id,design_group_id),
    CONSTRAINT product_family_onboarding_runs_family_scope_fk
        FOREIGN KEY (family_id,organization_id,design_group_id)
        REFERENCES product_families(id,organization_id,design_group_id),
    CONSTRAINT product_family_onboarding_runs_creator_scope_fk
        FOREIGN KEY (created_by,organization_id)
        REFERENCES actors(id,organization_id),
    CONSTRAINT product_family_onboarding_runs_analysis_check CHECK (
        (status = 'started' AND analysis IS NULL AND candidate_knowledge IS NULL)
        OR
        (status <> 'started' AND analysis IS NOT NULL
            AND analysis_sha256 ~ '^[0-9a-f]{64}$'
            AND btrim(analysis_path) <> ''
            AND candidate_knowledge IS NOT NULL
            AND package_sha256 ~ '^[0-9a-f]{64}$'
            AND btrim(package_path) <> ''
            AND analyzed_job_revision IS NOT NULL)
    ),
    UNIQUE(job_id),
    UNIQUE(job_id,id)
);

CREATE INDEX IF NOT EXISTS product_family_onboarding_runs_scope_idx
    ON product_family_onboarding_runs(
        organization_id,design_group_id,family_id,status,created_at,id
    );

CREATE TABLE IF NOT EXISTS product_family_onboarding_reviews (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL,
    job_id uuid NOT NULL,
    organization_id text NOT NULL,
    design_group_id text NOT NULL,
    family_id text NOT NULL,
    package_sha256 char(64) NOT NULL CHECK (package_sha256 ~ '^[0-9a-f]{64}$'),
    review_identity char(64) NOT NULL UNIQUE CHECK (
        review_identity ~ '^[0-9a-f]{64}$'
    ),
    decision text NOT NULL CHECK (decision IN ('approve','reject')),
    reviewer_id text NOT NULL,
    reviewer_text text NOT NULL CHECK (btrim(reviewer_text) <> ''),
    review_path text NOT NULL CHECK (btrim(review_path) <> ''),
    reviewed_job_revision integer NOT NULL CHECK (reviewed_job_revision >= 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT product_family_onboarding_reviews_run_scope_fk
        FOREIGN KEY (run_id,job_id)
        REFERENCES product_family_onboarding_runs(id,job_id),
    CONSTRAINT product_family_onboarding_reviews_job_scope_fk
        FOREIGN KEY (job_id,organization_id,design_group_id)
        REFERENCES design_jobs(id,organization_id,design_group_id),
    CONSTRAINT product_family_onboarding_reviews_family_scope_fk
        FOREIGN KEY (family_id,organization_id,design_group_id)
        REFERENCES product_families(id,organization_id,design_group_id),
    CONSTRAINT product_family_onboarding_reviews_reviewer_scope_fk
        FOREIGN KEY (reviewer_id,organization_id)
        REFERENCES actors(id,organization_id),
    UNIQUE(run_id)
);

CREATE TABLE IF NOT EXISTS product_family_onboarding_publications (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL UNIQUE,
    review_id uuid NOT NULL UNIQUE,
    job_id uuid NOT NULL,
    organization_id text NOT NULL,
    design_group_id text NOT NULL,
    family_id text NOT NULL,
    package_sha256 char(64) NOT NULL CHECK (package_sha256 ~ '^[0-9a-f]{64}$'),
    publication_identity char(64) NOT NULL UNIQUE CHECK (
        publication_identity ~ '^[0-9a-f]{64}$'
    ),
    publication_receipt_sha256 char(64) NOT NULL UNIQUE CHECK (
        publication_receipt_sha256 ~ '^[0-9a-f]{64}$'
    ),
    publication_path text NOT NULL CHECK (btrim(publication_path) <> ''),
    assertion_ids jsonb NOT NULL,
    published_job_revision integer NOT NULL CHECK (published_job_revision >= 0),
    published_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT product_family_onboarding_publications_run_scope_fk
        FOREIGN KEY (run_id,job_id)
        REFERENCES product_family_onboarding_runs(id,job_id),
    CONSTRAINT product_family_onboarding_publications_review_fk
        FOREIGN KEY (review_id) REFERENCES product_family_onboarding_reviews(id),
    CONSTRAINT product_family_onboarding_publications_job_scope_fk
        FOREIGN KEY (job_id,organization_id,design_group_id)
        REFERENCES design_jobs(id,organization_id,design_group_id),
    CONSTRAINT product_family_onboarding_publications_family_scope_fk
        FOREIGN KEY (family_id,organization_id,design_group_id)
        REFERENCES product_families(id,organization_id,design_group_id),
    CONSTRAINT product_family_onboarding_publications_publisher_scope_fk
        FOREIGN KEY (published_by,organization_id)
        REFERENCES actors(id,organization_id)
);

CREATE INDEX IF NOT EXISTS product_family_onboarding_publications_scope_idx
    ON product_family_onboarding_publications(
        organization_id,design_group_id,family_id,created_at,id
    );

CREATE OR REPLACE FUNCTION reject_product_family_onboarding_review_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'product family onboarding reviews are append-only';
END;
$$;

DROP TRIGGER IF EXISTS product_family_onboarding_reviews_append_only
    ON product_family_onboarding_reviews;
CREATE TRIGGER product_family_onboarding_reviews_append_only
    BEFORE UPDATE OR DELETE ON product_family_onboarding_reviews
    FOR EACH ROW EXECUTE FUNCTION reject_product_family_onboarding_review_mutation();

CREATE OR REPLACE FUNCTION reject_product_family_onboarding_publication_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'product family onboarding publications are append-only';
END;
$$;

DROP TRIGGER IF EXISTS product_family_onboarding_publications_append_only
    ON product_family_onboarding_publications;
CREATE TRIGGER product_family_onboarding_publications_append_only
    BEFORE UPDATE OR DELETE ON product_family_onboarding_publications
    FOR EACH ROW EXECUTE FUNCTION reject_product_family_onboarding_publication_mutation();
